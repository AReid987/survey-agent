"""Site adapter — abstract base class for survey site plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..browser.engine import BrowserPage
from ..credentials import CredentialStore
from ..profile.store import ProfileStore


@dataclass
class Survey:
    """A discovered survey."""

    id: str
    title: str
    url: str
    estimated_minutes: int = 0
    reward: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Question:
    """A survey question."""

    id: str
    text: str
    question_type: str  # radio | checkbox | text | textarea | select | scale | slider | date | matrix | open_ended
    options: list[str] | None = None
    required: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Answer:
    """An answer to a survey question."""

    question_id: str
    value: Any  # str for text, list[str] for checkbox, int for scale, etc.


@dataclass
class SiteBalance:
    """Balance on a survey site."""

    available: float
    pending: float
    currency: str = "USD"


@dataclass
class WithdrawalResult:
    """Result of a withdrawal attempt."""

    success: bool
    amount: float
    method: str
    message: str = ""


class SiteAdapter(ABC):
    """Abstract survey site adapter.

    Each survey site implements this interface to handle:
    - Login/logout
    - Survey discovery
    - Question parsing and answering
    - Balance checking and withdrawal
    - CAPTCHA detection
    """

    def __init__(
        self,
        credential_store: CredentialStore | None = None,
        profile_store: ProfileStore | None = None,
    ):
        self.credentials = credential_store or CredentialStore()
        self.profile = profile_store

    @property
    @abstractmethod
    def site_slug(self) -> str:
        """Unique identifier for this site (e.g. 'branded-surveys')."""
        ...

    @property
    @abstractmethod
    def base_url(self) -> str:
        """Base URL for the site."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable site name."""
        ...

    @abstractmethod
    async def login(self, page: BrowserPage) -> bool:
        """Log into the site. Returns True on success."""
        ...

    @abstractmethod
    async def get_surveys(self, page: BrowserPage) -> list[Survey]:
        """Discover available surveys on the dashboard."""
        ...

    @abstractmethod
    async def start_survey(self, page: BrowserPage, survey: Survey) -> bool:
        """Navigate to and start a survey. Returns True if survey loaded."""
        ...

    @abstractmethod
    async def get_current_question(self, page: BrowserPage) -> Question | None:
        """Parse the current question on the page. Returns None if survey is done."""
        ...

    @abstractmethod
    async def answer_question(self, page: BrowserPage, question: Question, answer: Answer) -> bool:
        """Submit an answer for the current question. Returns True on success."""
        ...

    @abstractmethod
    async def submit_survey(self, page: BrowserPage) -> bool:
        """Submit the completed survey. Returns True on success."""
        ...

    @abstractmethod
    async def get_balance(self, page: BrowserPage) -> SiteBalance:
        """Check the account balance."""
        ...

    @abstractmethod
    async def withdraw(self, page: BrowserPage, amount: float, method: str = "paypal") -> WithdrawalResult:
        """Attempt a withdrawal."""
        ...

    async def detect_captcha(self, page: BrowserPage) -> bool:
        """Detect if a CAPTCHA is present on the page. Override for site-specific detection."""
        return False

    async def is_logged_in(self, page: BrowserPage) -> bool:
        """Check if currently logged in. Override for site-specific detection."""
        return False

    async def logout(self, page: BrowserPage) -> None:
        """Logout. Override for site-specific logout flow."""
        pass
