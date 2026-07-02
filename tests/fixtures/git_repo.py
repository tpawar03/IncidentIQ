"""Builds a small, real multi-language git repo for AST-retriever tests (Task 13).

Not a pytest test module itself (no test_ prefix) — a builder imported BY the tests. Real
`git` plumbing (init/commit/rev-parse), so the retriever's clone-at-commit path is exercised
against actual git objects, not a mock.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_V1 = {
    "service.py": '''def get_user_balance(user_id):
    account = fetch_account(user_id)
    return account["balance"]


def fetch_account(user_id):
    return {"balance": 100, "pending": 0}
''',
    "main.py": '''from service import get_user_balance


def handle_request(user_id):
    return get_user_balance(user_id)
''',
}

# v2 introduces the "bug" (divide by a field that can be zero) in service.py, and adds one
# file per other supported language plus one UNSUPPORTED language (ruby) — all with the same
# shape so language detection / localization can be tested uniformly.
_V2_SERVICE_PY = '''def get_user_balance(user_id):
    account = fetch_account(user_id)
    return account["balance"] / account["pending"]


def fetch_account(user_id):
    return {"balance": 100, "pending": 0}
'''

_OTHER_LANGS = {
    "service.js": '''function getUserBalance(userId) {
    const account = fetchAccount(userId);
    return account.balance / account.pending;
}

function fetchAccount(userId) {
    return { balance: 100, pending: 0 };
}
''',
    "service.go": '''package main

type Account struct {
	Balance int
	Pending int
}

func GetUserBalance(userID string) int {
	account := FetchAccount(userID)
	return account.Balance / account.Pending
}

func FetchAccount(userID string) Account {
	return Account{Balance: 100, Pending: 0}
}
''',
    "Service.cs": '''public class Account
{
    public int Balance;
    public int Pending;
}

public class Service
{
    public int GetUserBalance(string userId)
    {
        var account = FetchAccount(userId);
        return account.Balance / account.Pending;
    }

    public Account FetchAccount(string userId)
    {
        return new Account { Balance = 100, Pending = 0 };
    }
}
''',
    "service.rs": '''struct Account {
    balance: i32,
    pending: i32,
}

fn get_user_balance(user_id: &str) -> i32 {
    let account = fetch_account(user_id);
    account.balance / account.pending
}

fn fetch_account(user_id: &str) -> Account {
    Account { balance: 100, pending: 0 }
}
''',
    "service.rb": '''def get_user_balance(user_id)
  account = fetch_account(user_id)
  account[:balance] / account[:pending]
end

def fetch_account(user_id)
  { balance: 100, pending: 0 }
end
''',
}


@dataclass
class FixtureRepo:
    path: Path
    v1_commit: str
    v2_commit: str


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def build_fixture_repo(tmp_path: Path) -> FixtureRepo:
    """Real git repo, 2 commits: v1 (clean get_user_balance) and v2 (divide-by-zero bug +
    other-language mirrors + one unsupported-language file). Returns the repo path and both
    commit SHAs so tests can clone-at-commit against either.
    """
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@incidentiq.local")
    _git(repo, "config", "user.name", "IncidentIQ Fixture")

    for name, content in _V1.items():
        (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "v1: clean get_user_balance")
    v1 = _git(repo, "rev-parse", "HEAD")

    (repo / "service.py").write_text(_V2_SERVICE_PY)
    for name, content in _OTHER_LANGS.items():
        (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "v2: divide-by-zero bug + other-language mirrors")
    v2 = _git(repo, "rev-parse", "HEAD")

    return FixtureRepo(path=repo, v1_commit=v1, v2_commit=v2)
