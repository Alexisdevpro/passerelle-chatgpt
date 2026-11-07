import ipaddress

from .errors import GatewayError


def client_ip(peer, forwarded, trusted_cidrs):
    networks = [ipaddress.ip_network(item) for item in trusted_cidrs]

    def trusted(value):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(address in network for network in networks)

    if not trusted(peer) or not forwarded:
        return peer
    if len(forwarded) > 2048 or len(forwarded.split(",")) > 16:
        raise GatewayError(400, "invalid_forwarded_header", "Chaîne de proxys invalide.")
    current = peer
    for value in reversed(forwarded.split(",")):
        if not trusted(current):
            break
        try:
            current = str(ipaddress.ip_address(value.strip()))
        except ValueError as exc:
            raise GatewayError(400, "invalid_forwarded_header", "Adresse de proxy invalide.") from exc
    return current
