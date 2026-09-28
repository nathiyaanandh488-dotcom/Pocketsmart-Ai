"""main.py - PocketSmart AI FastAPI backend.

Run with:  uvicorn main:app --reload
"""
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database
import gemini_utils
from auth import (
    COOKIE_NAME, TOKEN_EXPIRE_MINUTES, NotAuthenticated, authenticate_user,
    create_access_token, decode_token, get_current_user, get_optional_user, hash_password,
)

load_dotenv()

MIN_BUDGET, MAX_BUDGET = 500, 100_000_000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}

CATEGORIES = {
    "home": {"title": "Home interior plan", "planner": "/home-planner"},
    "party": {"title": "Party budget plan", "planner": "/party-planner"},
    "jewelry": {"title": "Jewelry picks", "planner": "/jewelry-planner"},
}

# Placeholder feedback for the demo. Replace with real reviews before publishing.
TESTIMONIALS = [
    {"name": "Sample user", "role": "Home planner",
     "text": "I entered 80,000 for a living room and got a lighting and sofa split I could act on the same evening."},
    {"name": "Sample user", "role": "Party planner",
     "text": "The catering per plate figure helped me negotiate with two caterers."},
    {"name": "Sample user", "role": "Jewelry planner",
     "text": "Uploading my saree photo gave me earrings that actually matched the border colour."},
]

SAMPLE_SPLIT = [
    {"category": "Catering", "amount": 22000, "percent": 44},
    {"category": "Decoration", "amount": 12500, "percent": 25},
    {"category": "Entertainment", "amount": 9000, "percent": 18},
    {"category": "Stay", "amount": 6500, "percent": 13},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create tables if they do not exist."""
    database.init_db()
    yield


app = FastAPI(title="PocketSmart AI", lifespan=lifespan)

origins = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=origins, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def inr(value) -> str:
    """Format a number as rupees with Indian digit grouping, e.g. 1,25,000."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return "\u20b90"
    digits = str(abs(n))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts + [tail])
    return f"{'-' if n < 0 else ''}\u20b9{digits}"


templates.env.filters["inr"] = inr


def render(request: Request, name: str, user: Optional[dict] = None,
           status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(
        request, name, {"user": user, "gemini_ready": gemini_utils.gemini_ready(), **ctx},
        status_code=status_code,
    )


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "Not authenticated"}, status_code=401)


def set_login_cookie(response, username: str) -> None:
    response.set_cookie(
        COOKIE_NAME, create_access_token(username), httponly=True, samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        max_age=TOKEN_EXPIRE_MINUTES * 60,
    )


def check_budget(budget: float) -> Optional[str]:
    if budget < MIN_BUDGET:
        return f"Enter a budget of at least \u20b9{MIN_BUDGET:,}."
    if budget > MAX_BUDGET:
        return "That budget is too large. Enter a smaller amount."
    return None


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    return render(request, "index.html", user, sample_split=SAMPLE_SPLIT)


@app.get("/testimonials", response_class=HTMLResponse)
def testimonials(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    return render(request, "testimonials.html", user, testimonials=TESTIMONIALS)


# ---------------------------------------------------------------------------
# Authentication: /register, /login, /logout, /token
# ---------------------------------------------------------------------------
@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "register.html")


@app.post("/register", response_class=HTMLResponse)
def register(request: Request, username: str = Form(...), email: str = Form(...),
             password: str = Form(...), confirm_password: str = Form(...)):
    username, email = username.strip(), email.strip()
    error = None
    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", username):
        error = "Use 3 to 30 letters, numbers or underscores for the username."
    elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        error = "Enter a valid email address."
    elif len(password) < 6:
        error = "Use a password of at least 6 characters."
    elif password != confirm_password:
        error = "The two passwords do not match."
    if error:
        return render(request, "register.html", error=error, form={"username": username, "email": email},
                      status_code=400)
    try:
        database.create_user(username, email, hash_password(password))
    except sqlite3.IntegrityError:
        return render(request, "register.html", error="That username or email is already registered.",
                      form={"username": username, "email": email}, status_code=409)
    response = RedirectResponse("/dashboard", status_code=303)
    set_login_cookie(response, username)
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = authenticate_user(username.strip(), password)
    if not user:
        return render(request, "login.html", error="Username or password is incorrect.",
                      form={"username": username}, status_code=401)
    response = RedirectResponse("/dashboard", status_code=303)
    set_login_cookie(response, user["username"])
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.post("/token")
def token(form: OAuth2PasswordRequestForm = Depends()):
    """Issue a JWT for API clients (send it as 'Authorization: Bearer <token>')."""
    user = authenticate_user(form.username, form.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    return {"access_token": create_access_token(user["username"]), "token_type": "bearer"}


@app.get("/session-info")
def session_info(request: Request):
    user = get_optional_user(request)
    if not user:
        return {"logged_in": False}
    token_value = request.cookies.get(COOKIE_NAME) or request.headers.get("Authorization", "")[7:]
    payload = decode_token(token_value) or {}
    return {"logged_in": True, "user_id": user["id"], "username": user["username"],
            "expires_at": payload.get("exp")}


@app.get("/session-data")
def session_data(user: dict = Depends(get_current_user)):
    recent = database.list_history(user["id"], limit=5)
    return {
        "username": user["username"],
        "counts": database.count_by_category(user["id"]),
        "recent": [{"id": h["id"], "category": h["category"], "budget": h["budget"],
                    "created_at": h["created_at"]} for h in recent],
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(get_current_user)):
    return render(request, "dashboard.html", user,
                  counts=database.count_by_category(user["id"]),
                  recent=database.list_history(user["id"], limit=5), categories=CATEGORIES)


# ---------------------------------------------------------------------------
# Planners
# ---------------------------------------------------------------------------
def show_result(request: Request, user: dict, category: str, budget: float,
                inputs: dict, result: dict) -> HTMLResponse:
    history_id = database.save_history(user["id"], category, budget, inputs, result)
    return render(request, "results.html", user, category=category, meta=CATEGORIES[category],
                  budget=budget, inputs=inputs, result=result, history_id=history_id)


# --- Home ---
@app.get("/home-planner", response_class=HTMLResponse)
def home_planner(request: Request, user: dict = Depends(get_current_user)):
    return render(request, "planner_home.html", user)


@app.post("/generate-home", response_class=HTMLResponse)
def generate_home(
    request: Request, user: dict = Depends(get_current_user),
    budget: float = Form(...), style: str = Form("Modern"),
    rooms: List[str] = Form(default=[]), notes: str = Form(""),
    lights: int = Form(0), ceiling_fans: int = Form(0), dining_tables: int = Form(0),
    sofas: int = Form(0), beds: int = Form(0), wall_art: int = Form(0),
):
    error = check_budget(budget)
    if error:
        return render(request, "planner_home.html", user, error=error, status_code=400)
    quantities = {"lights": lights, "ceiling_fans": ceiling_fans, "dining_tables": dining_tables,
                  "sofas": sofas, "beds": beds, "wall_art": wall_art}
    quantities = {k: max(0, min(v, 50)) for k, v in quantities.items()}
    inputs = {"style": style.strip()[:40], "rooms": rooms[:10], "quantities": quantities,
              "notes": notes.strip()[:500]}
    result = gemini_utils.generate_home_plan(budget, inputs)
    return show_result(request, user, "home", budget, inputs, result)


# --- Party ---
@app.get("/party-planner", response_class=HTMLResponse)
def party_planner(request: Request, user: dict = Depends(get_current_user)):
    return render(request, "planner_party.html", user)


@app.post("/generate-party", response_class=HTMLResponse)
def generate_party(
    request: Request, user: dict = Depends(get_current_user),
    budget: float = Form(...), guests: int = Form(...), event_type: str = Form("Birthday"),
    venue: str = Form(""), needs_stay: bool = Form(False), notes: str = Form(""),
):
    error = check_budget(budget)
    if not error and not 1 <= guests <= 5000:
        error = "Enter a guest count between 1 and 5,000."
    if error:
        return render(request, "planner_party.html", user, error=error, status_code=400)
    inputs = {"event_type": event_type.strip()[:40], "guests": guests, "venue": venue.strip()[:200],
              "needs_stay": needs_stay, "notes": notes.strip()[:500]}
    result = gemini_utils.generate_party_plan(budget, inputs)
    return show_result(request, user, "party", budget, inputs, result)


# --- Jewelry ---
@app.get("/jewelry-planner", response_class=HTMLResponse)
def jewelry_planner(request: Request, user: dict = Depends(get_current_user)):
    return render(request, "planner_jewelry.html", user)


@app.post("/generate-jewelry", response_class=HTMLResponse)
def generate_jewelry(
    request: Request, user: dict = Depends(get_current_user),
    budget: float = Form(...), occasion: str = Form("Party"), style: str = Form("Classic"),
    metal: str = Form("Any"), notes: str = Form(""),
    outfit_image: Optional[UploadFile] = File(None),
):
    error = check_budget(budget)
    image = None
    if not error and outfit_image and outfit_image.filename:
        if outfit_image.content_type not in ALLOWED_IMAGE_TYPES:
            error = "Upload a JPG, PNG or WebP image."
        else:
            data = outfit_image.file.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                error = "The image is larger than 5 MB. Upload a smaller one."
            else:
                image = (data, outfit_image.content_type)
    if error:
        return render(request, "planner_jewelry.html", user, error=error, status_code=400)
    inputs = {"occasion": occasion.strip()[:40], "style": style.strip()[:40],
              "metal": metal.strip()[:40], "notes": notes.strip()[:500],
              "image_used": image is not None}
    result = gemini_utils.generate_jewelry_plan(budget, inputs, image)
    return show_result(request, user, "jewelry", budget, inputs, result)


# ---------------------------------------------------------------------------
# History and detail views
# ---------------------------------------------------------------------------
@app.get("/history", response_class=HTMLResponse)
def history(request: Request, category: Optional[str] = None, user: dict = Depends(get_current_user)):
    if category not in CATEGORIES:
        category = None
    return render(request, "history.html", user, items=database.list_history(user["id"], category),
                  active=category, categories=CATEGORIES)


@app.get("/recommendation/{item_id}", response_class=HTMLResponse)
def recommendation_page(item_id: int, request: Request, user: dict = Depends(get_current_user)):
    item = database.get_history_item(user["id"], item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return render(request, "results.html", user, category=item["category"],
                  meta=CATEGORIES[item["category"]], budget=item["budget"], inputs=item["inputs"],
                  result=item["result"], history_id=item["id"], from_history=True)


@app.get("/recommendations-details")
def recommendations_details(id: Optional[int] = None, category: Optional[str] = None,
                            user: dict = Depends(get_current_user)):
    """JSON details: one saved recommendation (?id=) or the latest ones (?category=)."""
    if id is not None:
        item = database.get_history_item(user["id"], id)
        if not item:
            raise HTTPException(status_code=404, detail="Recommendation not found")
        return item
    if category and category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="category must be home, party or jewelry")
    return database.list_history(user["id"], category, limit=20)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
