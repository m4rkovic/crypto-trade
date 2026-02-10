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
        
        # Eksplicitna provera da li je testnet uključen u configu
        env_setting = self.cfg['system']['environment']
        is_testnet = str(env_setting).lower().strip() == 'testnet'
        
        self.logger.info(f"📡 TESTING EXCHANGE CONNECTIONS (Mode: {env_setting})...")

        for name, creds in self.cfg['exchanges'].items():
            client = None
            try:
                ex_class = getattr(ccxt, name)
                
                # --- 1. PRIPREMA KONFIGURACIJE ---
                exchange_config = {
                    'apiKey': creds['api_key'],
                    'secret': creds['secret'],
                    'password': creds.get('password', ''), 
                    'timeout': timeout,
                    'enableRateLimit': True,
                    'options': {'defaultType': 'spot'} 
                }

                # --- 2. FIX ZA BYBIT (Unified Account) ---
                if name == 'bybit':
                    exchange_config['options']['defaultType'] = 'unified'

                # --- 3. URL OVERRIDE U SAMOM STARTU (OVO JE KLJUČNO) ---
                # Ubacujemo URL-ove direktno u config pre kreiranja klijenta.
                # Ovako ccxt nema izbora nego da koristi ove adrese.
                if is_testnet:
                    exchange_config['sandbox'] = True 
                    
                    if name == 'binance':
                        exchange_config['urls'] = {
                            'api': {
                                'public': 'https://testnet.binance.vision/api',
                                'private': 'https://testnet.binance.vision/api',
                                'v3': 'https://testnet.binance.vision/api',
                                'spot': 'https://testnet.binance.vision/api',
                            }
                        }
                    elif name == 'bybit':
                        exchange_config['urls'] = {
                            'api': {
                                'public': 'https://api-testnet.bybit.com',
                                'private': 'https://api-testnet.bybit.com',
                                'spot': 'https://api-testnet.bybit.com',
                                'v5': 'https://api-testnet.bybit.com',
                                'unified': 'https://api-testnet.bybit.com',
                            }
                        }

                # --- 4. KREIRANJE KLIJENTA ---
                # Klijent se sada kreira sa već ubačenim Testnet URL-ovima
                client = ex_class(exchange_config)
                
                # --- 5. PROVERA GDE GAĐAMO ---
                api_urls = client.urls['api']
                target = "Unknown"
                if isinstance(api_urls, dict):
                    # Pokušavamo da izvučemo glavni URL za prikaz
                    target = api_urls.get('public', api_urls.get('v3', api_urls.get('spot', str(api_urls))))
                else:
                    target = api_urls
                
                self.logger.info(f"   ℹ️  {name.upper()} Target: {target}")

                # --- 6. KONEKCIJA I AUTH ---
                await client.load_markets()
                
                # Koristimo fetch_time za proveru konekcije jer je sigurnije od fetch_balance na početku
                if name in ['binance', 'bybit']:
                    await client.fetch_time()
                else:
                    await client.fetch_balance()
                
                self.exchanges[name] = client
                
                latency_info = client.last_response_headers.get('Date', 'OK')
                self.logger.info(f"   ✅ {name.upper():<10} | Latency: {latency_info} | Auth: OK")

            except ccxt.PermissionDenied as e:
                self.logger.critical(f"   ❌ {name.upper():<10} | PERMISSION DENIED: {str(e)}")
                all_connected = False
                if client: await client.close()

            except ccxt.AuthenticationError as e:
                self.logger.critical(f"   ❌ {name.upper():<10} | AUTH FAILED: {str(e)}")
                all_connected = False
                if client: await client.close()

            except ccxt.RequestTimeout as e:
                self.logger.error(f"   ❌ {name.upper():<10} | TIMEOUT: {str(e)}")
                all_connected = False
                if client: await client.close()
            
            except Exception as e:
                self.logger.critical(f"   ❌ {name.upper():<10} | ERROR: {str(e)}")
                all_connected = False
                if client: await client.close()

        return all_connected

    async def fetch_snapshots(self) -> List[TickerData]:
        return []

    async def shutdown(self):
        for ex in self.exchanges.values():
            await ex.close()