# API Keys and Authentication

This document details the API keys required to operate the arbitrage bot, how to obtain them, and how to configure them securely.

## Kalshi

Kalshi uses RSA-SHA256-PSS signatures to authenticate REST API requests.

### What You Need
1. `KALSHI_API_KEY_ID` (A standard UUID)
2. An RSA private key PEM file

### How to Obtain
1. Log into your Kalshi account.
2. Navigate to [Settings > API](https://kalshi.com/settings/api).
3. Generate a new API key. Kalshi will provide a Key ID and allow you to download or copy the RSA Private Key.

### Configuration
1. Save the RSA private key content to a file located at `config/kalshi_key.pem`. Ensure this file is ignored in version control (`.gitignore`).
2. Add your Key ID to the `.env` file:
   ```env
   KALSHI_API_KEY_ID=your-kalshi-key-id-uuid
   ```

## Polymarket US

The bot interfaces with the regulated Polymarket US API, which uses Ed25519 cryptography for request signing. **Note:** No Polygon wallet or passphrase is required for the US regulated version.

### What You Need
1. `POLYMARKET_US_KEY_ID` (A standard UUID)
2. `POLYMARKET_US_SECRET` (A base64 encoded Ed25519 private key)

### How to Obtain
1. Navigate to the Polymarket US Developer Portal at [https://polymarket.us/developer](https://polymarket.us/developer).
2. Create a new set of API credentials.
3. Securely store the provided Key ID and Secret.

### Configuration
Add the credentials to your `.env` file:
```env
POLYMARKET_US_KEY_ID=your-poly-key-id-uuid
POLYMARKET_US_SECRET=your-base64-ed25519-secret
```

### Technical Details
Requests to the base URL `https://api.polymarket.us` are authenticated using Ed25519 signed requests. The client automatically generates and includes the following headers:
- `X-PM-Access-Key`: The API Key ID
- `X-PM-Timestamp`: Current UNIX timestamp
- `X-PM-Signature`: The Ed25519 signature of the request payload

## Security Best Practices

- **Never Commit Secrets:** Ensure `.env` and `config/*.pem` files are included in your `.gitignore`.
- **File Permissions:** Restrict file permissions for your `.env` and `kalshi_key.pem` files (e.g., `chmod 600 .env` on Linux/macOS).
- **Rotate Keys:** Regularly rotate API keys and delete unused ones from the exchange platforms.
- **Limit Permissions:** If the exchanges support granular permissions, grant the API keys only the permissions necessary for trading and reading market data. No withdrawal permissions should be granted.
