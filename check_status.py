import asyncio
from src.config import load_config_from_yaml
from src.clients.polymarket_client import PolymarketClient
from src.clients.kalshi_client import KalshiClient

async def main():
    config = load_config_from_yaml()
    poly = PolymarketClient(config.api, dry_run=False)
    kalshi = KalshiClient(config.api, dry_run=False)
    
    await poly.connect()
    await kalshi.connect()
    
    pb = await poly.get_balance()
    kb = await kalshi.get_balance()
    
    print(f"Polymarket US Balance: ${pb:.2f}")
    print(f"Kalshi Balance: ${kb:.2f}")
    
    open_orders = await poly.get_open_orders()
    print(f"Polymarket Open Orders: {open_orders}")
    
    # Check open positions on Polymarket US
    try:
        path = "/v1/positions"
        headers = poly._get_us_auth_headers("GET", path)
        resp = await poly._us_client.get(path, headers=headers)
        print("Polymarket US Positions:", resp.status_code, resp.json() if resp.status_code == 200 else resp.text)
    # Check Kalshi positions
    try:
        resp = await kalshi._request("GET", "/portfolio/positions", authenticated=True)
        print("Kalshi Positions:", resp)
    except Exception as e:
        print("Kalshi positions check error:", e)
        
    await poly.close()
    await kalshi.close()

if __name__ == "__main__":
    asyncio.run(main())
