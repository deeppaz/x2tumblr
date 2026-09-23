"""Tumblr OAuth1 token'ını bir kez almak için (lokalde çalıştır)."""
from requests_oauthlib import OAuth1Session

key = input("Consumer key: ").strip()
secret = input("Consumer secret: ").strip()

s = OAuth1Session(key, client_secret=secret, callback_uri="https://localhost/")
s.fetch_request_token("https://www.tumblr.com/oauth/request_token")
print("\nTarayıcıda aç ve izin ver:\n" + s.authorization_url("https://www.tumblr.com/oauth/authorize"))
redirect = input("\nYönlendirildiğin URL'yi (localhost/...?oauth_verifier=...) yapıştır: ").strip()
s.parse_authorization_response(redirect)
tok = s.fetch_access_token("https://www.tumblr.com/oauth/access_token")

print("\nGitHub secrets:")
print("TUMBLR_OAUTH_TOKEN =", tok["oauth_token"])
print("TUMBLR_OAUTH_SECRET =", tok["oauth_token_secret"])
