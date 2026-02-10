# src/websocket_engine.py
import asyncio
import aiohttp
import json
import time
import logging
from typing import Dict, List, Callable, Awaitable
from .models import TickerData

class ExchangeStream:
    def __init__(self, symbols: List[str], callback: Callable[[TickerData], Awaitable[None]], testnet: bool = False, logger=None):
        self.symbols = symbols
        self.callback = callback
        self.testnet = testnet
        self.logger = logger

    async def connect(self, session: aiohttp.ClientSession):
        raise NotImplementedError

class BinanceStream(ExchangeStream):
    async def connect(self, session: aiohttp.ClientSession):
        streams = [f"{s.replace('/', '').lower()}@bookTicker" for s in self.symbols]
        
        if self.testnet:
            base_url = "wss://stream.testnet.binance.vision/ws"
        else:
            base_url = "wss://stream.binance.com:9443/ws"
            
        url = f"{base_url}/{'/'.join(streams)}"
        
        try:
            async with session.ws_connect(url) as ws:
                if self.logger: self.logger.info(f"✅ Connected to BINANCE WS: {base_url}")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        raw_s = data.get('s', '').upper()
                        std_sym = raw_s.replace("USDT", "/USDT") if "USDT" in raw_s else raw_s
                        
                        ticker = TickerData(
                            exchange="binance",
                            symbol=std_sym,
                            bid_price=float(data.get('b', 0.0)),
                            bid_vol=float(data.get('B', 0.0)),
                            ask_price=float(data.get('a', 0.0)),
                            ask_vol=float(data.get('A', 0.0)),
                            timestamp=time.time() 
                        )
                        await self.callback(ticker)
        except Exception as e:
            if self.logger: self.logger.error(f"BINANCE WS ERROR: {e}")

class OkxStream(ExchangeStream):
    async def connect(self, session: aiohttp.ClientSession):
        url = "wss://ws.okx.com:8443/ws/v5/public"
        
        async with session.ws_connect(url) as ws:
            if self.logger: self.logger.info(f"✅ Connected to OKX WS: {url}")
            args = [{"channel": "tickers", "instId": s.replace('/', '-')} for s in self.symbols]
            await ws.send_json({"op": "subscribe", "args": args})

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if 'data' in data and data['data']:
                        t = data['data'][0]
                        bid_p = t.get('bidPx')
                        ask_p = t.get('askPx')
                        
                        ticker = TickerData(
                            exchange="okx",
                            symbol=t['instId'].replace('-', '/'),
                            bid_price=float(bid_p) if bid_p else 0.0,
                            bid_vol=float(t.get('bidSz', 0.0)),
                            ask_price=float(ask_p) if ask_p else 0.0,
                            ask_vol=float(t.get('askSz', 0.0)),
                            timestamp=int(t.get('ts', time.time()*1000)) / 1000.0
                        )
                        await self.callback(ticker)

class BybitStream(ExchangeStream):
    async def connect(self, session: aiohttp.ClientSession):
        if self.testnet:
            url = "wss://stream-testnet.bybit.com/v5/public/spot"
        else:
            url = "wss://stream.bybit.com/v5/public/spot"
            
        try:
            async with session.ws_connect(url) as ws:
                if self.logger: self.logger.info(f"✅ Connected to BYBIT WS: {url}")
                
                args = [f"tickers.{s.replace('/', '')}" for s in self.symbols]
                req = {"op": "subscribe", "args": args}
                await ws.send_json(req)

                async def heartbeat():
                    while True:
                        await asyncio.sleep(20)
                        try:
                            await ws.send_json({"op": "ping"})
                        except:
                            break

                heartbeat_task = asyncio.create_task(heartbeat())

                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            
                            if 'topic' in data and 'tickers' in data['topic']:
                                t = data['data']
                                raw_s = data['topic'].split('.')[1]
                                std_sym = raw_s.replace("USDT", "/USDT")

                                # --- FIX: FALLBACK NA LAST PRICE ---
                                bid_p = t.get('bid1Price')
                                if not bid_p:
                                    bid_p = t.get('lastPrice', 0.0)
                                    
                                ask_p = t.get('ask1Price')
                                if not ask_p:
                                    ask_p = t.get('lastPrice', 0.0)

                                ticker = TickerData(
                                    exchange="bybit",
                                    symbol=std_sym,
                                    bid_price=float(bid_p),
                                    bid_vol=float(t.get('bid1Size', 0.0)),
                                    ask_price=float(ask_p),
                                    ask_vol=float(t.get('ask1Size', 0.0)),
                                    timestamp=int(data.get('ts', time.time()*1000)) / 1000.0
                                )
                                await self.callback(ticker)
                finally:
                    heartbeat_task.cancel()

        except Exception as e:
            if self.logger: self.logger.error(f"BYBIT WS ERROR: {e}")

class WebSocketEngine:
    def __init__(self, exchanges: List[str], coins: List[str], logger, strategy_callback, testnet: bool = False):
        self.exchanges = exchanges
        self.coins = coins
        self.logger = logger
        self.strategy_callback = strategy_callback
        self.testnet = testnet
        self.running = False
        self.tasks = []

    async def _relay_ticker(self, ticker: TickerData):
        await self.strategy_callback(ticker)

    async def start(self):
        self.running = True
        session = aiohttp.ClientSession()
        
        streams = []
        if 'binance' in self.exchanges: 
            streams.append(BinanceStream(self.coins, self._relay_ticker, self.testnet, self.logger))
        if 'bybit' in self.exchanges: 
            streams.append(BybitStream(self.coins, self._relay_ticker, self.testnet, self.logger))
        if 'okx' in self.exchanges: 
            streams.append(OkxStream(self.coins, self._relay_ticker, self.testnet, self.logger))
            
        self.logger.info(f"⚡ WS Engine Starting ({'TESTNET' if self.testnet else 'LIVE'}): {len(streams)} streams...")
        self.tasks = [asyncio.create_task(self._keep_alive(s, session)) for s in streams]

    async def _keep_alive(self, stream, session):
        while self.running:
            try:
                await stream.connect(session)
            except Exception as e:
                if self.logger: self.logger.error(f"WS Disconnected ({type(stream).__name__}): {e}. Retry in 5s...")
                await asyncio.sleep(5)

    async def shutdown(self):
        self.running = False
        for t in self.tasks: t.cancel()