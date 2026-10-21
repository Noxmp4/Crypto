"""Authentification : Cloudflare Access (+ vérification du JWT par le site lui-même).

Cloudflare Access filtre déjà les visiteurs avant le tunnel. On revérifie ici le jeton
`Cf-Access-Jwt-Assertion` (signature RS256 avec les clés publiques de l'équipe,
audience de l'application, émetteur) : si le tunnel était un jour exposé sans Access,
le site refuserait quand même tout accès. Sans configuration, le site refuse tout
(fermé par défaut), sauf en mode développement local explicite.
"""
from __future__ import annotations

import hashlib
import hmac

import jwt


class AuthError(Exception):
    pass


class AccessVerifier:
    def __init__(self, team_domain: str, audience: str, jwks_client=None):
        team = team_domain.strip().removeprefix("https://").rstrip("/")
        self.issuer = f"https://{team}"
        self.audience = audience
        self.jwks = jwks_client or jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs",
                                                   cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> dict:
        if not token:
            raise AuthError("jeton Cloudflare Access absent")
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iat", "aud"]})
        except jwt.PyJWTError as e:
            raise AuthError(f"jeton invalide : {e}") from e
        if not claims.get("email"):
            raise AuthError("jeton sans adresse e-mail")
        return claims


def csrf_token(secret: str, email: str) -> str:
    return hmac.new(secret.encode(), f"csrf:{email}".encode(), hashlib.sha256).hexdigest()


def csrf_ok(secret: str, email: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(csrf_token(secret, email), token)
