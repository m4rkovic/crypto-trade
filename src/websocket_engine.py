import asyncio
import aiohttp
import json
import time
import logging
from typing import Dict, List, Callable
from .models import TickerData

class ExchangeStream:
    def __init__(self, symbols: List[str], callback: Callable):
        self.symbols = symbols
        self.callback = callback
        self.ws = None

    async def connect(self, session: aiohttp.ClientSession):
        raise NotImplementedError

class BinanceStream(ExchangeStream):
    async def connect(self, session: aiohttp.ClientSession):
        # Format: btcusdt@bookTicker / ethusdt@bookTicker
        streams = [f"{s.replace('/', '').lower()}@bookTicker" for s in self.symbols]
        url = f"wss://stream.binance.com:9443/ws/{'/'.join(streams)}"
        
        async with session.ws_connect(url) as ws:
            self.ws = ws
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    # Binance stream payload is just the ticker object
                    # We need to map symbol back (e.g., 'solusdt' -> 'SOL/USDT')
                    # For speed, we just normalize incoming symbol
                    raw_sym = data['s'].upper() 
                    # Quick hack to insert slash for standard format
                    if "USDT" in raw_sym:
                        std_sym = raw_sym.replace("USDT", "/USDT")
                    else:
                        std_sym = raw_sym

                    ticker = TickerData(
                        exchange="binance",
                        symbol=std_sym,
                        bid_price=float(data['b']),
                        bid_vol=float(data['B']),
                        ask_price=float(data['a']),
                        ask_vol=float(data['A']),
                        timestamp=time.time()
                    )
                    await self.callback(ticker)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break

class OkxStream(ExchangeStream):
    async def connect(self, session: aiohttp.ClientSession):
        url = "wss://ws.okx.com:8443/ws/v5/public"
        async with session.ws_connect(url) as ws:
            self.ws = ws
            
            # OKX Format: BTC-USDT
            args = []
            for s in self.symbols:
                args.append({"channel": "tickers", "instId": s.replace('/', '-')})
                
            await ws.send_json({"op": "subscribe", "args": args})

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if 'data' in data:
                        t = data['data'][0]
                        # OKX returns timestamp in ms string
                        ticker = TickerData(
                            exchange="okx",
                            symbol=t['instId'].replace('-', '/'),
                            bid_price=float(t['bidPx']),
                            bid_vol=float(t['bidSz']),
                            ask_price=float(t['askPx']),
                            ask_vol=float(t['askSz']),
                            timestamp=time.time()
                        )
                        await self.callback(ticker)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break

class BybitStream(ExchangeStream):
    async def connect(self, session: aiohttp.ClientSession):
        url = "wss://stream.bybit.com/v5/public/spot"
        async with session.ws_connect(url) as ws:
            self.ws = ws
            
            # Bybit Format: BTCUSDT
            args = [f"tickers.{s.replace('/', '')}" for s in self.symbols]
            
            # Bybit V5 Req ID is optional but good practice
            await ws.send_json({"op": "subscribe", "args": args, "req_id": "1001"})

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if 'topic' in data and 'tickers' in data['topic']:
                        t = data['data']
                        raw_sym = data['topic'].split('.')[1]
                        std_sym = raw_sym.replace("USDT", "/USDT")

                        ticker = TickerData(
                            exchange="bybit",
                            symbol=std_sym,
                            bid_price=float(t['bid1Price']),
                            bid_vol=float(t['bid1Size']),
                            ask_price=float(t['ask1Price']),
                            ask_vol=float(t['ask1Size']),
                            timestamp=time.time()
                        )
                        await self.callback(ticker)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break

class WebSocketEngine:
    def __init__(self, active_exchanges: List[str], active_coins: List[str], logger):
        self.exchanges = active_exchanges
        self.coins = active_coins
        self.logger = logger
        # Cache: { 'SOL/USDT': { 'binance': TickerData, 'okx': TickerData } }
        self.latest_data: Dict[str, Dict[str, TickerData]] = {}
        for c in active_coins:
            self.latest_data[c] = {}
            
        self.running = False
        self._session = None
        self.tasks = []

    async def _handle_update(self, ticker: TickerData):
        if ticker.symbol in self.latest_data:
            self.latest_data[ticker.symbol][ticker.exchange] = ticker

    async def start(self):
        self.running = True
        self._session = aiohttp.ClientSession()
        
        streams = []
        if 'binance' in self.exchanges:
            streams.append(BinanceStream(self.coins, self._handle_update))
        if 'okx' in self.exchanges:
            streams.append(OkxStream(self.coins, self._handle_update))
        if 'bybit' in self.exchanges:
            streams.append(BybitStream(self.coins, self._handle_update))
            
        self.logger.info(f"⚡ CONNECTING {len(streams)} STREAMS FOR {len(self.coins)} COINS...")
        self.tasks = [asyncio.create_task(self._run_stream_forever(s)) for s in streams]

    async def _run_stream_forever(self, stream):
        while self.running:
            try:
                await stream.connect(self._session)
            except Exception as e:
                self.logger.error(f"WS Error: {e}")
            if self.running:
                await asyncio.sleep(2)

    def get_snapshot(self, symbol: str) -> List[TickerData]:
        return list(self.latest_data.get(symbol, {}).values())
        
    def get_all_prices(self) -> Dict[str, float]:
        """Returns simplified price map for Dashboard: {'BTC': 50000, 'SOL': 20}"""
        prices = {}
        for coin, data in self.latest_data.items():
            # Just take the first available price found
            if data:
                first_ex = list(data.keys())[0]
                prices[coin.split('/')[0]] = data[first_ex].ask_price
        return prices

    async def shutdown(self):
        self.running = False
        if self._session: await self._session.close()
        for t in self.tasks: t.cancel()