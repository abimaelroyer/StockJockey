import os
import sys
import pyotp
import robin_stocks.robinhood as r

def main():
    # Retrieve credentials securely from environment variables
    username = os.environ.get('ROBINHOOD_USER')
    password = os.environ.get('ROBINHOOD_PASS')
    mfa_secret = os.environ.get('ROBINHOOD_MFA_SECRET')

    if not username or not password or not mfa_secret:
        print("Missing credentials. Please set ROBINHOOD_USER, ROBINHOOD_PASS, and ROBINHOOD_MFA_SECRET.")
        sys.exit(1)

    try:
        # Generate time-based one-time password (TOTP)
        totp = pyotp.TOTP(mfa_secret).now()
        
        # Authenticate with Robinhood
        r.login(username, password, mfa_code=totp)
        
        # Verify connection by pulling a single stock quote
        quote = r.get_latest_price('AAPL')
        if quote:
            print(f"Login successful! AAPL current quote: ${quote[0]}")
        else:
            print("Login succeeded, but failed to retrieve AAPL quote.")
            
    except Exception as e:
        print(f"Robinhood connection failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()