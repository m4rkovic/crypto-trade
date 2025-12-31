# src/market_engine.py
import ccxt.async_support as ccxt
import asyncio
from typing import Dict, List
from .models import TickerData

class MarketEngine:
    """
    Manages REST API connections to exchanges.
    Responsible for initial diagnostics, authentication verification,
    and providing the exchange clients for Execution and Inventory engines.
    """
    def __init__(self, config: dict, logger):
        self.exchanges: Dict[str, ccxt.Exchange] = {}
        self.cfg = config
        self.logger = logger

    async def initialize(self) -> bool:
        """
        Connects to exchanges and performs a robust connectivity test.
        Returns False if ANY exchange fails the diagnostic.
        """
        timeout = self.cfg['performance']['network_timeout_ms']
        all_connected = True
        
        self.logger.info("📡 TESTING EXCHANGE CONNECTIONS...")

        for name, creds in self.cfg['exchanges'].items():
            try:
                ex_class = getattr(ccxt, name)
                client = ex_class({
                    'apiKey': creds['api_key'],
                    'secret': creds['secret'],
                    'password': creds.get('password', ''), # OKX/KuCoin require password
                    'timeout': timeout,
                    'enableRateLimit': True,
                    'options': {'defaultType': 'spot'}
                })
                
                if self.cfg['system']['environment'] == 'testnet':
                    client.set_sandbox_mode(True)
                
                # --- DIAGNOSTIC PHASE 1: PUBLIC API ---
                # Checks internet connection and exchange status
                await client.load_markets()
                
                # --- DIAGNOSTIC PHASE 2: PRIVATE API ---
                # Checks API Key validity and Permissions
                # We try to fetch a specific balance to prove we are authenticated
                await client.fetch_balance({'type': 'spot'})
                
                self.exchanges[name] = client
                # Extract server time header for latency check if available
                latency_info = client.last_response_headers.get('Date', 'OK')
                self.logger.info(f"   ✅ {name.upper():<10} | Latency: {latency_info} | Auth: OK")

            except ccxt.AuthenticationError as e:
                self.logger.critical(f"   ❌ {name.upper():<10} | AUTH FAILED: Invalid API Key or Secret.")
                all_connected = False
                await client.close()

            except ccxt.PermissionDenied as e:
                self.logger.critical(f"   ❌ {name.upper():<10} | PERMISSION DENIED: Key missing 'Spot Trading' or 'IP Whitelist' permissions.")
                all_connected = False
                await client.close()

            except ccxt.AccountSuspended as e:
                self.logger.critical(f"   ❌ {name.upper():<10} | ACCOUNT SUSPENDED: Contact support immediately.")
                all_connected = False
                await client.close()

            except ccxt.RequestTimeout as e:
                self.logger.error(f"   ❌ {name.upper():<10} | TIMEOUT: Exchange API is slow or down.")
                all_connected = False
                await client.close()
            
            except ccxt.ExchangeNotAvailable as e:
                self.logger.error(f"   ❌ {name.upper():<10} | MAINTENANCE: Exchange is currently offline.")
                all_connected = False
                await client.close()

            except Exception as e:
                self.logger.critical(f"   ❌ {name.upper():<10} | UNKNOWN ERROR: {str(e)}")
                all_connected = False
                await client.close()

        return all_connected

    async def fetch_snapshots(self) -> List[TickerData]:
        """
        Legacy/Fallback method.
        In this V2 architecture, data comes from WebSocketEngine, 
        so this returns an empty list to satisfy interface requirements if called.
        """
        return []

    async def shutdown(self):
        """
        Gracefully closes all REST API sessions.
        """
        for ex in self.exchanges.values():
            await ex.close()