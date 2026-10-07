from datetime import datetime, timedelta
from typing import Callable, List, Optional, Union

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, field_validator
from pydantic_core import core_schema
from sqlalchemy.orm import selectinload
from sqlmodel import Field, Relationship, Session, SQLModel, select

from project_name.models.content import Content, ContentResponse

from .config import settings
from .db import engine

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

SECRET_KEY = settings.security.secret_key
ALGORITHM = settings.security.algorithm


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class RefreshToken(BaseModel):
    refresh_token: str


class TokenData(BaseModel):
    username: Optional[str] = None


class HashedPassword(str):
    """Takes a plain text password and hashes it.

    use this as a field in your SQLModel

    class User(SQLModel, table=True):
        username: str
        password: HashedPassword

    """

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        return core_schema.no_info_before_validator_function(
            cls.validate, core_schema.str_schema()
        )

    @classmethod
    def validate(cls, v):
        """Accepts a plain text password and returns a hashed password."""
        if not isinstance(v, str):
            raise TypeError("string required")

        hashed_password = get_password_hash(v)
        # you could also return a string here which would mean model.password
        # would be a string, pydantic won't care but you could end up with some
        # confusion since the value's type won't match the type annotation
        # exactly
        return cls(hashed_password)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(sa_column_kwargs={"unique": True})
    password: HashedPassword
    superuser: bool = False
    disabled: bool = False

    # it populates the .user attribute on the Content Model
    contents: List["Content"] = Relationship(back_populates="user")

    @field_validator("password", mode="before")
    @classmethod
    def _hash_plain_password(cls, value):
        # SQLModel table models bypass the custom type schema of
        # HashedPassword, so hashing is enforced here instead.
        if isinstance(value, str) and not value.startswith("$2"):
            return get_password_hash(value)
        return value


class UserResponse(BaseModel):
    """This is the User model to be used as a response_model
    it doesn't include the password.
    """

    id: int
    username: str
    disabled: bool
    superuser: bool
    contents: Optional[List[ContentResponse]] = Field(default_factory=list)


class UserCreate(BaseModel):
    """This is the User model to be used when creating a new user."""

    username: str
    password: str
    superuser: bool = False
    disabled: bool = False


class UserPasswordPatch(SQLModel):
    """This is to accept password for changing"""

    password: str
    password_confirm: str


def verify_password(plain_password, hashed_password) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password) -> str:
    return pwd_context.hash(password)


def create_access_token(
    data: dict, expires_delta: Optional[timedelta] = None
) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire, "scope": "access_token"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(
    data: dict, expires_delta: Optional[timedelta] = None
) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire, "scope": "refresh_token"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def authenticate_user(
    get_user: Callable, username: str, password: str
) -> Union[User, bool]:
    user = get_user(username)
    if not user:
        return False
    if not verify_password(password, user.password):
        return False
    return user


def get_user(username) -> Optional[User]:
    with Session(engine) as session:
        # eager load contents so the detached instance can be serialized
        return session.exec(
            select(User)
            .where(User.username == username)
            .options(selectinload(User.contents))
        ).first()


def get_current_user(
    token: str = Depends(oauth2_scheme), request: Request = None, fresh=False
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if request:
        if authorization := request.headers.get("authorization"):
            try:
                token = authorization.split(" ")[1]
            except IndexError:
                raise credentials_exception

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")

        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception
    user = get_user(username=token_data.username)
    if user is None:
        raise credentials_exception
    if fresh and (not payload["fresh"] and not user.superuser):
        raise credentials_exception

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.disabled:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


AuthenticatedUser = Depends(get_current_active_user)


def get_current_fresh_user(
    token: str = Depends(oauth2_scheme), request: Request = None
) -> User:
    return get_current_user(token, request, True)


AuthenticatedFreshUser = Depends(get_current_fresh_user)


async def get_current_admin_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not an admin user"
        )
    return current_user


AdminUser = Depends(get_current_admin_user)


async def validate_token(token: str = Depends(oauth2_scheme)) -> User:

    user = get_current_user(token=token)
    return user
