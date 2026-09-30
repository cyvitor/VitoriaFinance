import bcrypt
from passlib.context import CryptContext

# O bcrypt 4.1+ tirou o atributo que o passlib consulta e passou a recusar
# senhas acima de 72 bytes, o que impede até o teste interno da biblioteca.
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type("about", (), {"__version__": getattr(bcrypt, "__version__", "4")})
_bcrypt_hashpw = bcrypt.hashpw


def _hashpw(password, salt):
    if isinstance(password, (bytes, bytearray)) and len(password) > 72:
        password = bytes(password[:72])
    return _bcrypt_hashpw(password, salt)


bcrypt.hashpw = _hashpw

password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)

