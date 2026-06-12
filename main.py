"""
Cricket Bot — Complete Merged Version
Features: team join, live lobby, 2-min timer, captain selection, toss,
overs selection, /select_batter, /select_bowler, /shot, DM /bowl,
team-size wicket limit, innings change, winner, /score, /endmatch
"""

import logging
import random
import asyncio
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────── BOT TOKEN ───────────────────────────
BOT_TOKEN = "8886228595:AAG5KlddR6PI4cxeiUDeY70TzxHq2sNADSA"   # ← Replace with your token

# Welcome image — same folder mein welcome.png rakho
WELCOME_IMAGE = os.path.join(os.path.dirname(__file__), "welcome.png")

# ─────────────────────────── CONSTANTS ───────────────────────────
JOIN_TIMEOUT        = 120           # seconds for lobby
DELIVERY_TIMEOUT    = 120           # seconds for batter/bowler to respond
PENALTY_RUNS        = 5             # penalty deducted for timeout
MIN_PLAYERS_PER_TEAM = 2
MAX_OVERS_OPTIONS   = [1, 2, 5, 10, 20]

SHOT_OUTCOMES = {
    "defensive": [0, 0, 0, 1, 1, 2],
    "drive":     [0, 1, 2, 4, 4, 6],
    "sweep":     [0, 1, 4, 4, 6, "W"],
    "pull":      [1, 2, 4, 6, 6, "W"],
    "slog":      [0, 4, 6, 6, "W", "W"],
    "loft":      [0, 2, 4, 6, "W", "W"],
}

# ───────────────────────── GAME STATE ────────────────────────────
# games[chat_id] = { ... full match state ... }
games: dict[int, dict] = {}


def new_game(chat_id: int) -> dict:
    return {
        "chat_id":        chat_id,
        "phase":          "lobby",       # lobby→captain→toss→overs→batting→innings2→ended
        "teams":          {"A": [], "B": []},
        "captains":       {"A": None, "B": None},
        "team_names":     {"A": "Team A", "B": "Team B"},
        "join_msg_id":    None,
        "toss_winner":    None,
        "batting_team":   None,
        "bowling_team":   None,
        "max_overs":      None,
        "innings":        1,
        # per-innings scorecards
        "scores":         {1: {"A": 0, "B": 0}, 2: {"A": 0, "B": 0}},
        "wickets":        {1: {"A": 0, "B": 0}, 2: {"A": 0, "B": 0}},
        "balls":          {1: {"A": 0, "B": 0}, 2: {"A": 0, "B": 0}},
        # current over/delivery context
        "batter":         None,          # striker user_id
        "non_striker":    None,          # non-striker user_id
        "bowler":         None,          # user_id of current bowler
        "bowler_choice":  None,          # pending bowl choice from DM
        "shot_choice":    None,
        "waiting_for":    None,          # "shot" | "bowl"
        "target":         None,          # set after innings 1
        # lobby join tracking (user_id → team)
        "joined":         {},
        "lobby_task":     None,
        "delivery_task":  None,   # 2-min shot/bowl timeout task
        # out players per innings — inn → set of user_ids
        "out_batters":    {1: set(), 2: set()},
        # host — match ka admin
        "host":           None,   # (user_id, username)
        # player stats for Player of the Match
        "stats":          {},     # uid → {runs, balls_faced, wickets, balls_bowled}
    }


def get_game(chat_id: int) -> dict | None:
    return games.get(chat_id)


def is_host(g: dict, user_id: int) -> bool:
    """Check karo ke user host hai ya nahi."""
    return g["host"] is not None and g["host"][0] == user_id


def update_stats(g: dict, batter_uid: int, bowler_uid: int, result):
    """Har delivery ke baad stats update karo."""
    s = g["stats"]
    if batter_uid not in s:
        s[batter_uid] = {"runs": 0, "balls_faced": 0, "wickets": 0, "balls_bowled": 0}
    if bowler_uid not in s:
        s[bowler_uid] = {"runs": 0, "balls_faced": 0, "wickets": 0, "balls_bowled": 0}

    s[batter_uid]["balls_faced"] += 1
    s[bowler_uid]["balls_bowled"] += 1

    if result == "W":
        s[bowler_uid]["wickets"] += 1
    else:
        s[batter_uid]["runs"] += result


def get_potm(g: dict) -> tuple:
    """Sabse best player choose karo — runs + wickets*20 score se."""
    best_uid  = None
    best_score = -1
    all_players = g["teams"]["A"] + g["teams"]["B"]

    for uid, uname in all_players:
        st = g["stats"].get(uid, {})
        score = st.get("runs", 0) + st.get("wickets", 0) * 20
        if score > best_score:
            best_score = score
            best_uid   = (uid, uname)

    return best_uid, best_score





def batting_label(g: dict) -> str:
    return g["batting_team"]


def bowling_label(g: dict) -> str:
    return g["bowling_team"]


def current_score(g: dict) -> tuple[int, int, int]:
    inn  = g["innings"]
    bt   = g["batting_team"]
    runs = g["scores"][inn][bt]
    wkts = g["wickets"][inn][bt]
    balls= g["balls"][inn][bt]
    return runs, wkts, balls


def max_wickets(g: dict) -> int:
    """Wicket limit = team size - 1 (last man stands)."""
    bt = g["batting_team"]
    return max(1, len(g["teams"][bt]) - 1)


def overs_done(g: dict) -> bool:
    inn   = g["innings"]
    bt    = g["batting_team"]
    balls = g["balls"][inn][bt]
    return balls >= g["max_overs"] * 6


def wickets_done(g: dict) -> bool:
    return g["wickets"][g["innings"]][g["batting_team"]] >= max_wickets(g)


def target_chased(g: dict) -> bool:
    if g["innings"] == 2 and g["target"] is not None:
        runs, _, _ = current_score(g)
        return runs >= g["target"]


def innings_over(g: dict) -> bool:
    return overs_done(g) or wickets_done(g) or target_chased(g)


def scoreboard_text(g: dict) -> str:
    bt  = g.get("batting_team")
    bl  = g.get("bowling_team")
    inn = g["innings"]

    lines = ["📊 *Live Scorecard*\n━━━━━━━━━━━━━━━━━━━━"]

    for i in [1, 2]:
        if g["innings"] < i:
            break
        for team in ["A", "B"]:
            r    = g["scores"][i].get(team, 0)
            w    = g["wickets"][i].get(team, 0)
            b    = g["balls"][i].get(team, 0)
            ov   = f"{b//6}.{b%6}"
            name = g["team_names"][team]
            arrow = " ◀ batting" if (i == inn and team == bt) else ""
            lines.append(f"🏏 Inn{i} *{name}*: {r}/{w} ({ov} ov){arrow}")

    lines.append("━━━━━━━━━━━━━━━━━━━━")

    if g["phase"] == "batting" and bt:
        striker_name     = next((u[1] for u in g["teams"][bt] if u[0] == g.get("batter")),       "—")
        non_striker_name = next((u[1] for u in g["teams"][bt] if u[0] == g.get("non_striker")),  "—")
        bowler_name      = next((u[1] for u in g["teams"][bl] if u[0] == g.get("bowler")),       "—") if bl else "—"
        lines.append(f"⚡ Striker: *{striker_name}* 🏏")
        lines.append(f"🔄 Non-striker: *{non_striker_name}*")
        lines.append(f"🎯 Bowler: *{bowler_name}*")
        lines.append("━━━━━━━━━━━━━━━━━━━━")

    if g["innings"] == 2 and g.get("target"):
        runs, _, _ = current_score(g)
        need = max(0, g["target"] - runs)
        lines.append(f"🎯 Target: {g['target']} | Need: {need} more runs")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  LOBBY PHASE
# ═══════════════════════════════════════════════════════════════

async def cmd_startmatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in games:
        await update.message.reply_text("⚠️ A match is already running. Use /endmatch first.")
        return

    user    = update.effective_user
    g       = new_game(chat_id)
    games[chat_id] = g

    # /startmatch karne wala automatically host ban jaata hai
    g["host"] = (user.id, user.username or user.first_name)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join Team A", callback_data="join_A"),
         InlineKeyboardButton("🔴 Join Team B", callback_data="join_B")],
        [InlineKeyboardButton("✅ Start Match", callback_data="force_start")]
    ])
    msg = await update.message.reply_text(
        f"🏟️ *Cricket Match Lobby*\n\n"
        f"👑 Host: *{user.username or user.first_name}*\n\n"
        f"Join a team! Match starts in *2 minutes* or when started manually.",
        reply_markup=kb, parse_mode="Markdown"
    )
    g["join_msg_id"] = msg.message_id

    # auto-start after JOIN_TIMEOUT
    g["lobby_task"] = asyncio.create_task(
        lobby_countdown(chat_id, msg.message_id, ctx.application)
    )


async def lobby_countdown(chat_id: int, msg_id: int, app: Application):
    await asyncio.sleep(JOIN_TIMEOUT)
    g = games.get(chat_id)
    if g and g["phase"] == "lobby":
        await try_start_match(chat_id, app.bot, forced=False)


async def update_lobby_message(g: dict, bot: Bot):
    # teams mein (uid, username) tuples hain
    a_names = [uname for _, uname in g["teams"]["A"]]
    b_names = [uname for _, uname in g["teams"]["B"]]

    a_str = ", ".join(a_names) if a_names else "—"
    b_str = ", ".join(b_names) if b_names else "—"

    host_name = g["host"][1] if g["host"] else "—"
    text = (
        f"🏟️ *Cricket Match Lobby*\n"
        f"👑 Host: *{host_name}*\n\n"
        f"🔵 *Team A* ({len(a_names)} players)\n{a_str}\n\n"
        f"🔴 *Team B* ({len(b_names)} players)\n{b_str}\n\n"
        f"⏳ Match starts in 2 min or click ✅ Start Match"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join Team A", callback_data="join_A"),
         InlineKeyboardButton("🔴 Join Team B", callback_data="join_B")],
        [InlineKeyboardButton("✅ Start Match", callback_data="force_start")]
    ])
    try:
        await bot.edit_message_text(
            text,
            chat_id=g["chat_id"],
            message_id=g["join_msg_id"],
            reply_markup=kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Lobby message update failed: {e}")


async def cb_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    g = get_game(chat_id)

    if not g or g["phase"] != "lobby":
        await query.answer("Lobby band ho gayi!", show_alert=True)
        return

    user     = query.from_user
    user_id  = user.id
    username = user.username or user.first_name
    team     = query.data.split("_")[1]   # "A" or "B"

    if user_id in g["joined"]:
        prev = g["joined"][user_id]
        if prev == team:
            await query.answer(f"Tum pehle se Team {team} mein ho!", show_alert=True)
            return
        # doosri team se nikalo
        g["teams"][prev] = [(u, n) for u, n in g["teams"][prev] if u != user_id]

    g["joined"][user_id] = team
    g["teams"][team].append((user_id, username))

    # pehle message update karo, phir answer karo
    await update_lobby_message(g, ctx.bot)
    await query.answer(f"✅ Team {team} mein join ho gaye!")


async def cb_force_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    g = get_game(chat_id)
    if not g or g["phase"] != "lobby":
        return
    await try_start_match(chat_id, ctx.bot, forced=True)


async def try_start_match(chat_id: int, bot: Bot, forced: bool):
    g = games.get(chat_id)
    if not g or g["phase"] != "lobby":
        return

    a, b = g["teams"]["A"], g["teams"]["B"]
    if len(a) < MIN_PLAYERS_PER_TEAM or len(b) < MIN_PLAYERS_PER_TEAM:
        if forced:
            await bot.send_message(
                chat_id,
                f"⚠️ Need at least {MIN_PLAYERS_PER_TEAM} players per team to start."
            )
        else:
            await bot.send_message(chat_id, "⏰ Time's up! Not enough players — match cancelled.")
            del games[chat_id]
        return

    if g["lobby_task"]:
        g["lobby_task"].cancel()

    g["phase"] = "captain"
    await bot.send_message(chat_id, "✅ Teams locked! Now selecting captains…")
    await ask_captain(chat_id, bot, "A")


# ═══════════════════════════════════════════════════════════════
#  CAPTAIN SELECTION
# ═══════════════════════════════════════════════════════════════

async def ask_captain(chat_id: int, bot: Bot, team: str):
    g = games[chat_id]
    players = g["teams"][team]
    name = g["team_names"][team]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{uname}", callback_data=f"cap_{team}_{uid}")]
        for uid, uname in players
    ])
    await bot.send_message(
        chat_id, f"👑 *{name}* — select your captain:",
        reply_markup=kb, parse_mode="Markdown"
    )


async def cb_captain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    g = get_game(chat_id)
    if not g or g["phase"] != "captain":
        return

    _, team, uid_str = query.data.split("_")
    uid = int(uid_str)
    voter = query.from_user.id

    # only team members vote
    team_ids = [u[0] for u in g["teams"][team]]
    if voter not in team_ids:
        await query.answer("You're not in this team!", show_alert=True)
        return

    uname = next(u[1] for u in g["teams"][team] if u[0] == uid)
    g["captains"][team] = (uid, uname)

    await query.edit_message_text(f"👑 *{g['team_names'][team]}* captain: *{uname}*", parse_mode="Markdown")

    if team == "A" and g["captains"]["B"] is None:
        await ask_captain(chat_id, ctx.bot, "B")
    elif team == "B" and g["captains"]["A"] is not None:
        # both captains set → toss
        await do_toss(chat_id, ctx.bot)


# ═══════════════════════════════════════════════════════════════
#  TOSS
# ═══════════════════════════════════════════════════════════════

async def do_toss(chat_id: int, bot: Bot):
    g = games[chat_id]
    g["phase"] = "toss"
    cap_a_id, cap_a_name = g["captains"]["A"]

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🪙 Heads", callback_data="toss_heads"),
        InlineKeyboardButton("🪙 Tails", callback_data="toss_tails"),
    ]])
    await bot.send_message(
        chat_id,
        f"🪙 *Toss Time!*\n\n{cap_a_name} ({g['team_names']['A']}) — call it:",
        reply_markup=kb, parse_mode="Markdown"
    )


async def cb_toss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    g = get_game(chat_id)
    if not g or g["phase"] != "toss":
        return

    caller = query.from_user.id
    cap_a_id = g["captains"]["A"][0]
    if caller != cap_a_id:
        await query.answer("Only Team A captain calls the toss!", show_alert=True)
        return

    call   = query.data.split("_")[1]   # heads / tails
    result = random.choice(["heads", "tails"])
    won    = (call == result)
    winner_team = "A" if won else "B"
    g["toss_winner"] = winner_team

    winner_name = g["team_names"][winner_team]
    await query.edit_message_text(
        f"🪙 Coin shows *{result.upper()}*!\n\n"
        f"{'✅ Correct call!' if won else '❌ Wrong call!'}\n"
        f"*{winner_name}* wins the toss!",
        parse_mode="Markdown"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏏 Bat First", callback_data="choose_bat"),
        InlineKeyboardButton("🎳 Bowl First", callback_data="choose_bowl"),
    ]])
    await ctx.bot.send_message(
        chat_id,
        f"*{winner_name}* — choose to bat or bowl first:",
        reply_markup=kb, parse_mode="Markdown"
    )


async def cb_choose_innings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    g = get_game(chat_id)
    if not g or g["phase"] != "toss":
        return

    caller = query.from_user.id
    tw = g["toss_winner"]
    cap_tw_id = g["captains"][tw][0]
    if caller != cap_tw_id:
        await query.answer("Only the toss winner can choose!", show_alert=True)
        return

    choice = query.data  # "choose_bat" or "choose_bowl"
    if choice == "choose_bat":
        g["batting_team"] = tw
        g["bowling_team"] = "B" if tw == "A" else "A"
    else:
        g["bowling_team"] = tw
        g["batting_team"] = "B" if tw == "A" else "A"

    bt_name = g["team_names"][g["batting_team"]]
    bl_name = g["team_names"][g["bowling_team"]]
    await query.edit_message_text(
        f"*{bt_name}* will bat first.\n*{bl_name}* will bowl first.",
        parse_mode="Markdown"
    )
    await ask_overs(chat_id, ctx.bot)


# ═══════════════════════════════════════════════════════════════
#  OVERS SELECTION
# ═══════════════════════════════════════════════════════════════

async def ask_overs(chat_id: int, bot: Bot):
    g = games[chat_id]
    g["phase"] = "overs"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{o} ov", callback_data=f"overs_{o}")
        for o in MAX_OVERS_OPTIONS
    ]])
    await bot.send_message(
        chat_id, "⚙️ *Select number of overs per innings:*",
        reply_markup=kb, parse_mode="Markdown"
    )


async def cb_overs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    g = get_game(chat_id)
    if not g or g["phase"] != "overs":
        return

    caller = query.from_user.id
    # any captain can set overs
    cap_ids = [g["captains"]["A"][0], g["captains"]["B"][0]]
    if caller not in cap_ids:
        await query.answer("Only captains can set overs!", show_alert=True)
        return

    overs = int(query.data.split("_")[1])
    g["max_overs"] = overs
    await query.edit_message_text(f"✅ Match set to *{overs} overs* per innings.", parse_mode="Markdown")
    await start_innings(chat_id, ctx.bot)


# ═══════════════════════════════════════════════════════════════
#  INNINGS START
# ═══════════════════════════════════════════════════════════════

async def start_innings(chat_id: int, bot: Bot):
    g = games[chat_id]
    g["phase"] = "batting"
    g["batter"] = None
    g["non_striker"] = None
    g["bowler"] = None
    g["bowler_choice"] = None
    g["shot_choice"] = None
    g["waiting_for"] = None
    # Reset out batters for current innings only
    g["out_batters"][g["innings"]] = set()

    inn = g["innings"]
    bt_name = g["team_names"][g["batting_team"]]
    bl_name = g["team_names"][g["bowling_team"]]
    wk_limit = max_wickets(g)

    if inn == 2:
        target = g["scores"][1][g["batting_team"]] + 1
        # batting team in inn2 is the team that bowled inn1
        # re-derive target correctly
        # team that batted inn1:
        inn1_batter = g["batting_team"]   # already swapped before calling start_innings
        target = g["scores"][1][inn1_batter] + 1
        # Actually after swap batting_team is inn2 batting team; inn1 batting team is now bowling team
        inn1_bat = g["bowling_team"]   # bowling in inn2 = batted in inn1
        target = g["scores"][1][inn1_bat] + 1
        g["target"] = target

        # apply any pre-penalties from innings 1 bowler timeouts
        pre_pen = g.get("pre_penalties", {}).get(g["batting_team"], 0)
        if pre_pen:
            g["scores"][2][g["batting_team"]] = max(0, g["scores"][2][g["batting_team"]] - pre_pen)
            pen_note = f"\n⚠️ Pre-penalty applied: -{pre_pen} runs (bowler timeout in Inn 1)"
        else:
            pen_note = ""

        await bot.send_message(
            chat_id,
            f"🔁 *Innings 2 begins!*\n\n"
            f"*{bt_name}* needs *{target}* runs to win in {g['max_overs']} overs.\n"
            f"Wicket limit: {wk_limit}{pen_note}",
            parse_mode="Markdown"
        )
    else:
        await bot.send_message(
            chat_id,
            f"🏏 *Innings 1 begins!*\n\n"
            f"*{bt_name}* batting | *{bl_name}* bowling\n"
            f"Overs: {g['max_overs']} | Wicket limit: {wk_limit}",
            parse_mode="Markdown"
        )
    await ask_select_batter(chat_id, bot)


async def ask_select_batter(chat_id: int, bot: Bot):
    g = games[chat_id]
    bt  = g["batting_team"]
    inn = g["innings"]
    players  = g["teams"][bt]
    cap_name = g["captains"][bt][1]

    # Exclude: non-striker (already at crease) + already out players
    ns      = g.get("non_striker")
    out_set = g["out_batters"][inn]
    available = [
        (uid, uname) for uid, uname in players
        if uid != ns and uid not in out_set
    ]

    if not available:
        # Koi batter nahi bacha — innings khatam
        await bot.send_message(chat_id, "🏁 Koi batter nahi bacha! Innings khatam.", parse_mode="Markdown")
        await handle_innings_end(chat_id, bot)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏏 {uname}", callback_data=f"selbatter_{uid}")]
        for uid, uname in available
    ])
    label = "⚡ *Nayi striker* select karo (wicket ke baad):" if out_set else "⚡ *Striker* select karo:"
    await bot.send_message(
        chat_id,
        f"*{cap_name}* — {label}",
        reply_markup=kb, parse_mode="Markdown"
    )


async def ask_select_non_striker(chat_id: int, bot: Bot):
    g = games[chat_id]
    bt  = g["batting_team"]
    inn = g["innings"]
    players  = g["teams"][bt]
    cap_name = g["captains"][bt][1]

    striker = g.get("batter")
    out_set = g["out_batters"][inn]
    available = [
        (uid, uname) for uid, uname in players
        if uid != striker and uid not in out_set
    ]

    if not available:
        await bot.send_message(chat_id, "⚠️ Non-striker ke liye koi player nahi bacha!", parse_mode="Markdown")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔄 {uname}", callback_data=f"selnonstriker_{uid}")]
        for uid, uname in available
    ])
    await bot.send_message(
        chat_id,
        f"*{cap_name}* — 🔄 *Non-striker* select karo:",
        reply_markup=kb, parse_mode="Markdown"
    )


async def ask_select_bowler(chat_id: int, bot: Bot):
    g = games[chat_id]
    bl = g["bowling_team"]
    players = g["teams"][bl]
    cap_id  = g["captains"][bl][0]
    cap_name= g["captains"][bl][1]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(uname, callback_data=f"selbowler_{uid}")]
        for uid, uname in players
    ])
    await bot.send_message(
        chat_id,
        f"*{cap_name}* — select your bowler:",
        reply_markup=kb, parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════
#  /select_batter command
# ═══════════════════════════════════════════════════════════════

async def cmd_select_batter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g or g["phase"] != "batting":
        return
    bt = g["batting_team"]
    cap_id = g["captains"][bt][0]
    if update.effective_user.id != cap_id:
        await update.message.reply_text("Only the batting captain can use this.")
        return
    await ask_select_batter(chat_id, ctx.bot)


async def cb_select_batter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    g = get_game(chat_id)
    if not g:
        return

    bt = g["batting_team"]
    cap_id = g["captains"][bt][0]
    if query.from_user.id != cap_id:
        await query.answer("Only batting captain can select!", show_alert=True)
        return

    uid = int(query.data.split("_")[1])
    uname = next(u[1] for u in g["teams"][bt] if u[0] == uid)
    g["batter"] = uid
    await query.edit_message_text(f"⚡ Striker: *{uname}*", parse_mode="Markdown")

    # If non-striker not set yet (innings start), ask for non-striker
    if g.get("non_striker") is None:
        await ask_select_non_striker(chat_id, ctx.bot)
    elif g["bowler"] is None:
        await ask_select_bowler(chat_id, ctx.bot)
    else:
        await prompt_delivery(chat_id, ctx.bot)


async def cb_select_non_striker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    g = get_game(chat_id)
    if not g:
        return

    bt = g["batting_team"]
    cap_id = g["captains"][bt][0]
    if query.from_user.id != cap_id:
        await query.answer("Only batting captain can select!", show_alert=True)
        return

    uid = int(query.data.split("_")[1])
    uname = next(u[1] for u in g["teams"][bt] if u[0] == uid)
    g["non_striker"] = uid
    await query.edit_message_text(f"🔄 Non-striker: *{uname}*", parse_mode="Markdown")

    if g["bowler"] is None:
        await ask_select_bowler(chat_id, ctx.bot)
    else:
        await prompt_delivery(chat_id, ctx.bot)


# ═══════════════════════════════════════════════════════════════
#  /select_bowler command
# ═══════════════════════════════════════════════════════════════

async def cmd_select_bowler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g or g["phase"] != "batting":
        return
    bl = g["bowling_team"]
    cap_id = g["captains"][bl][0]
    if update.effective_user.id != cap_id:
        await update.message.reply_text("Only the bowling captain can use this.")
        return
    await ask_select_bowler(chat_id, ctx.bot)


async def cb_select_bowler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    g = get_game(chat_id)
    if not g:
        return

    bl = g["bowling_team"]
    cap_id = g["captains"][bl][0]
    if query.from_user.id != cap_id:
        await query.answer("Only bowling captain can select!", show_alert=True)
        return

    uid = int(query.data.split("_")[1])
    uname = next(u[1] for u in g["teams"][bl] if u[0] == uid)
    g["bowler"] = uid
    await query.edit_message_text(f"🎳 Bowler selected: *{uname}*", parse_mode="Markdown")

    if g["batter"] is None:
        await ask_select_batter(chat_id, ctx.bot)
    else:
        await prompt_delivery(chat_id, ctx.bot)


# ═══════════════════════════════════════════════════════════════
#  DELIVERY PROMPT
# ═══════════════════════════════════════════════════════════════

async def prompt_delivery(chat_id: int, bot: Bot):
    g = games[chat_id]
    runs, wkts, balls = current_score(g)
    ov = f"{balls//6}.{balls%6}"
    bt_name = g["team_names"][g["batting_team"]]

    batter_uid  = g["batter"]
    bowler_uid  = g["bowler"]
    batter_data = next(u for u in g["teams"][g["batting_team"]] if u[0] == batter_uid)
    bowler_data = next(u for u in g["teams"][g["bowling_team"]] if u[0] == bowler_uid)

    # HTML mention tags — ye Telegram mein user ko tag karta hai
    batter_tag = f'<a href="tg://user?id={batter_data[0]}">{batter_data[1]}</a>'
    bowler_tag = f'<a href="tg://user?id={bowler_data[0]}">{bowler_data[1]}</a>'

    target_txt = ""
    if g["innings"] == 2 and g["target"]:
        need = g["target"] - runs
        target_txt = f"\n🎯 Need: <b>{need}</b> runs | Target: <b>{g['target']}</b>"

    bot_info     = await bot.get_me()
    bot_username = bot_info.username

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🎯 Bowl Now — {bowler_data[1]}",
            url=f"https://t.me/{bot_username}?start=bowl"
        )
    ]])

    await bot.send_message(
        chat_id,
        f"🏏 <b>{bt_name}</b> {runs}/{wkts} ({ov} ov){target_txt}\n\n"
        f"⚡ Striker: {batter_tag}\n"
        f"🎯 Bowler: {bowler_tag}\n\n"
        f"<b>Step 1️⃣</b> — {bowler_tag} 👇 Button dabao → DM mein <b>1-6</b> type karo\n"
        f"<b>Step 2️⃣</b> — {batter_tag} group mein sirf <b>0-6</b> number type karo\n\n"
        f"💡 Same number = <b>WICKET!</b> | Alag = batter ke runs\n"
        f"⏳ <b>{DELIVERY_TIMEOUT//60} min</b> mein respond karo warna OUT + -{PENALTY_RUNS} penalty!",
        parse_mode="HTML",
        reply_markup=kb
    )
    g["waiting_for"] = "both"
    g["shot_choice"]  = None
    g["bowler_choice"]= None

    if g.get("delivery_task") and not g["delivery_task"].done():
        g["delivery_task"].cancel()

    g["delivery_task"] = asyncio.create_task(
        delivery_timeout(chat_id, bot)
    )


async def delivery_timeout(chat_id: int, bot: Bot):
    """Called after DELIVERY_TIMEOUT seconds if batter/bowler didn't respond."""
    await asyncio.sleep(DELIVERY_TIMEOUT)
    g = games.get(chat_id)
    if not g or g["phase"] != "batting":
        return

    inn = g["innings"]
    bt  = g["batting_team"]
    bl  = g["bowling_team"]

    batter_timed_out = g["shot_choice"] is None
    bowler_timed_out = g["bowler_choice"] is None

    messages = []

    if batter_timed_out and g["batter"] is not None:
        batter_name = next((u[1] for u in g["teams"][bt] if u[0] == g["batter"]), "Batter")
        out_uid = g["batter"]
        # wicket + penalty
        g["wickets"][inn][bt] += 1
        g["scores"][inn][bt]  = max(0, g["scores"][inn][bt] - PENALTY_RUNS)
        g["balls"][inn][bt]   += 1
        # OUT hue batter ko dobara select na ho
        g["out_batters"][inn].add(out_uid)
        messages.append(
            f"⏰ *{batter_name}* ne 2 min mein shot nahi khela!\n"
            f"💥 OUT + *-{PENALTY_RUNS} runs* penalty {g['team_names'][bt]} ko!"
        )
        g["batter"] = None

    if bowler_timed_out and g["bowler"] is not None:
        bowler_name = next((u[1] for u in g["teams"][bl] if u[0] == g["bowler"]), "Bowler")
        # penalty to bowling team
        g["scores"][inn][bt]  = g["scores"][inn].get(bt, 0)  # no run change for batting
        # penalty: deduct from bowling team's score in their innings
        # find which innings bowling team batted
        bl_bat_inn = None
        for i in [1, 2]:
            if i != inn:
                bl_bat_inn = i
                break
        if bl_bat_inn:
            g["scores"][bl_bat_inn][bl] = max(0, g["scores"][bl_bat_inn].get(bl, 0) - PENALTY_RUNS)
        else:
            # bowling team hasn't batted yet, store as a pre-penalty
            g.setdefault("pre_penalties", {})[bl] = g.get("pre_penalties", {}).get(bl, 0) + PENALTY_RUNS
        messages.append(
            f"⏰ *{bowler_name}* ne 2 min mein bowl nahi kiya!\n"
            f"💥 *-{PENALTY_RUNS} runs* penalty {g['team_names'][bl]} ko!"
        )
        g["bowler"] = None

    if messages:
        runs, wkts, balls = current_score(g)
        ov = f"{balls//6}.{balls%6}"
        msg = "\n\n".join(messages)
        msg += f"\n\n📊 {g['team_names'][bt]}: {runs}/{wkts} ({ov} ov)"
        await bot.send_message(chat_id, msg, parse_mode="Markdown")

    # check innings over after penalty
    if innings_over(g):
        await handle_innings_end(chat_id, bot)
        return

    # re-select as needed
    if g["batter"] is None and g["bowler"] is None:
        await ask_select_batter(chat_id, bot)
    elif g["batter"] is None:
        await ask_select_batter(chat_id, bot)
    elif g["bowler"] is None:
        await ask_select_bowler(chat_id, bot)
    else:
        await prompt_delivery(chat_id, bot)


# ═══════════════════════════════════════════════════════════════
#  PLAIN NUMBER INPUT  — batter & bowler sirf number type karenge
# ═══════════════════════════════════════════════════════════════

async def handle_number_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Group mein koi 0-6 type kare → batter ka input.
    DM mein koi 1-6 type kare  → bowler ka input.
    """
    text = update.message.text.strip()
    if not text.isdigit():
        return
    num = int(text)
    user_id  = update.effective_user.id
    chat_type = update.effective_chat.type

    # ── DM mein bowler ka input ──────────────────────────────
    if chat_type == "private":
        if num < 1 or num > 6:
            await update.message.reply_text("❌ 1 se 6 ke beech number daalo!")
            return

        target_game = None
        for cid, g in games.items():
            if g["phase"] == "batting" and g["bowler"] == user_id:
                target_game = g
                break

        if not target_game:
            return  # not an active bowler, ignore

        if target_game["bowler_choice"] is not None:
            await update.message.reply_text("⚠️ Tumne pehle se number daal diya! Batter ka wait karo.")
            return

        target_game["bowler_choice"] = num
        await update.message.reply_text(
            f"✅ Delivery lock: *{num}*\n📢 Batter ko notify kar diya!",
            parse_mode="Markdown"
        )

        # Group mein batter ko notify karo
        chat_id     = target_game["chat_id"]
        batter_data = next(
            (u for u in target_game["teams"][target_game["batting_team"]]
             if u[0] == target_game["batter"]), None
        )
        if batter_data:
            batter_tag = f'<a href="tg://user?id={batter_data[0]}">{batter_data[1]}</a>'
            await ctx.bot.send_message(
                chat_id,
                f"🎯 <b>Bowler ne bowl kar diya!</b>\n\n"
                f"⚡ {batter_tag} — ab apna number type karo (0-6)!",
                parse_mode="HTML"
            )
        return

    # ── Group mein batter ka input ───────────────────────────
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g or g["phase"] != "batting":
        return
    if user_id != g["batter"]:
        return  # koi aur type kar raha hai, ignore

    if num < 0 or num > 6:
        await update.message.reply_text("❌ 0 se 6 ke beech number daalo!")
        return

    if g["bowler_choice"] is None:
        await update.message.reply_text("⏳ Pehle bowler bowl karega! Bowler ka wait karo…")
        return

    if g["shot_choice"] is not None:
        await update.message.reply_text("⚠️ Tumne pehle se number daal diya!")
        return

    g["shot_choice"] = num
    batter_name = next(u[1] for u in g["teams"][g["batting_team"]] if u[0] == g["batter"])
    await update.message.reply_text(
        f"✅ *{batter_name}*: *{num}*",
        parse_mode="Markdown"
    )

    if g.get("delivery_task") and not g["delivery_task"].done():
        g["delivery_task"].cancel()
    await resolve_delivery(chat_id, ctx.bot)


# Keep /bat and /bowl as fallback commands (show hint)
async def cmd_bat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💡 Ab command ki zaroorat nahi!\nSirf *0-6* mein se koi number type karo group mein.",
        parse_mode="Markdown"
    )


async def cmd_bowl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("🔒 Bowl karne ke liye bot ka DM kholo aur wahan number type karo (1-6)!")
        return
    await update.message.reply_text(
        "💡 Command ki zaroorat nahi!\nSirf *1-6* mein se koi number type karo yahan DM mein.",
        parse_mode="Markdown"
    )






# ═══════════════════════════════════════════════════════════════
#  GIF URLs  (seedhe Telegram-compatible direct .gif/.mp4 links)
# ═══════════════════════════════════════════════════════════════

# Aap inhe apne pasand ke GIFs se replace kar sakte ho
GIFS = {
    "four": [
        "https://media.tenor.com/videos/9b1c2e1b2e1b2e1b/mp4",   # placeholder
        "https://i.imgur.com/boundary4.gif",
    ],
    "six": [
        "https://media.tenor.com/videos/abc123/mp4",               # placeholder
        "https://i.imgur.com/six_hit.gif",
    ],
    "out": [
        "https://media.tenor.com/videos/xyz789/mp4",               # placeholder
        "https://i.imgur.com/wicket.gif",
    ],
}

# ── Real working GIFs (Tenor direct links) ──────────────────────
GIFS = {
    "four": [
        "https://media.tenor.com/EHqFBIBLkXoAAAAC/cricket-four.gif",
        "https://media.tenor.com/9UKPpNRGldMAAAAC/cricket-boundary.gif",
        "https://media.tenor.com/oqU9j8GqWJ8AAAAC/cricket-shot.gif",
    ],
    "six": [
        "https://media.tenor.com/TvFCh8dGbNsAAAAC/cricket-six.gif",
        "https://media.tenor.com/rIq9QBYqR5YAAAAC/cricket-sixer.gif",
        "https://media.tenor.com/6mfDyuZm_XAAAAAC/six-cricket.gif",
    ],
    "out": [
        "https://media.tenor.com/yCdEMoqMRXoAAAAC/cricket-out.gif",
        "https://media.tenor.com/g8XjyqLGAPIAAAAC/cricket-wicket.gif",
        "https://media.tenor.com/pQlHxHoYH3EAAAAC/cricket-bowled.gif",
    ],
}


async def send_gif(bot: Bot, chat_id: int, gif_type: str, caption: str):
    """Send a random GIF for the given type (four/six/out). Falls back to text if GIF fails."""
    url = random.choice(GIFS[gif_type])
    try:
        await bot.send_animation(chat_id, animation=url, caption=caption, parse_mode="Markdown")
    except Exception:
        # if GIF fails (bad URL etc.), just send text
        await bot.send_message(chat_id, caption, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
#  RESOLVE DELIVERY
# ═══════════════════════════════════════════════════════════════

async def resolve_delivery(chat_id: int, bot: Bot):
    g = games[chat_id]
    bat_num  = g["shot_choice"]   # batter ka number (0-6)
    bowl_num = g["bowler_choice"] # bowler ka number (1-6)
    inn = g["innings"]
    bt  = g["batting_team"]

    batter_name = next(u[1] for u in g["teams"][bt] if u[0] == g["batter"])
    bowler_name = next(u[1] for u in g["teams"][g["bowling_team"]] if u[0] == g["bowler"])

    # ── MATCHING LOGIC ──────────────────────────────────────────
    # Agar dono same number → WICKET (odd match) ya runs (even)
    # Agar alag → batter ka number = runs scored
    if bat_num == bowl_num:
        result = "W"
    else:
        result = bat_num

    g["balls"][inn][bt] += 1

    # Stats update
    update_stats(g, g["batter"], g["bowler"], result)

    if result == "W":
        out_uid = g["batter"]   # OUT hone wale ka id save karo PEHLE
        g["wickets"][inn][bt] += 1
        runs, wkts, balls = current_score(g)
        ov = f"{balls//6}.{balls%6}"
        ns_name = next((u[1] for u in g["teams"][bt] if u[0] == g.get("non_striker")), "—")
        caption = (
            f"💥 *WICKET!*\n\n"
            f"*{batter_name}* is OUT!\n"
            f"Batter: *{bat_num}* | Bowler: *{bowl_num}* — Same number!\n\n"
            f"📊 {g['team_names'][bt]}: {runs}/{wkts} ({ov} ov)\n"
            f"🔄 Non-striker: *{ns_name}* continues"
        )
        await send_gif(bot, chat_id, "out", caption)
        # OUT hue batter ko list mein add karo — dobara select nahi hoga
        g["out_batters"][inn].add(out_uid)
        g["batter"] = None
        # non-striker stays, new striker needed

    elif result == 6:
        g["scores"][inn][bt] += 6
        runs, wkts, balls = current_score(g)
        ov = f"{balls//6}.{balls%6}"
        # 6 = no strike change (hitter stays on strike)
        caption = (
            f"🔥 *SIX!* Maximum!\n\n"
            f"Batter: *{bat_num}* | Bowler: *{bowl_num}*\n"
            f"📊 {g['team_names'][bt]}: {runs}/{wkts} ({ov} ov)"
        )
        await send_gif(bot, chat_id, "six", caption)

    elif result == 4:
        g["scores"][inn][bt] += 4
        runs, wkts, balls = current_score(g)
        ov = f"{balls//6}.{balls%6}"
        # 4 = no strike change
        caption = (
            f"✨ *FOUR!* Boundary!\n\n"
            f"Batter: *{bat_num}* | Bowler: *{bowl_num}*\n"
            f"📊 {g['team_names'][bt]}: {runs}/{wkts} ({ov} ov)"
        )
        await send_gif(bot, chat_id, "four", caption)

    else:
        g["scores"][inn][bt] += result
        runs, wkts, balls = current_score(g)
        ov = f"{balls//6}.{balls%6}"
        # Odd runs (1,3,5) → strike changes
        strike_changed = result in [1, 2, 3, 5] and result % 2 == 1
        if strike_changed:
            g["batter"], g["non_striker"] = g["non_striker"], g["batter"]
            new_striker_data = next((u for u in g["teams"][bt] if u[0] == g["batter"]), None)
            if new_striker_data:
                new_striker_tag = f'<a href="tg://user?id={new_striker_data[0]}">{new_striker_data[1]}</a>'
                strike_note = f"\n🔄 Strike change! {new_striker_tag} now on strike"
            else:
                strike_note = ""
        else:
            strike_note = ""
        emoji = "🏃" if result > 0 else "🛡️"
        await bot.send_message(
            chat_id,
            f"{emoji} <b>{result} run{'s' if result != 1 else ''}!</b>\n\n"
            f"Batter: <b>{bat_num}</b> | Bowler: <b>{bowl_num}</b>\n"
            f"📊 {g['team_names'][bt]}: {runs}/{wkts} ({ov} ov){strike_note}",
            parse_mode="HTML"
        )

    # check innings over
    if innings_over(g):
        await handle_innings_end(chat_id, bot)
        return

    # new over?
    balls_after = g["balls"][inn][bt]
    if balls_after % 6 == 0:
        # End of over → strike rotates
        g["batter"], g["non_striker"] = g["non_striker"], g["batter"]
        new_striker = next((u[1] for u in g["teams"][bt] if u[0] == g["batter"]), "—")
        await bot.send_message(
            chat_id,
            f"🔔 *Over {balls_after//6} complete!*\n\n"
            f"🔄 Strike change — *{new_striker}* now on strike next over",
            parse_mode="Markdown"
        )
        g["bowler"] = None
        await ask_select_bowler(chat_id, bot)
        return

    # need new batter?
    if g["batter"] is None:
        await ask_select_batter(chat_id, bot)
        return

    # continue
    await prompt_delivery(chat_id, bot)


# ═══════════════════════════════════════════════════════════════
#  INNINGS CHANGE / END
# ═══════════════════════════════════════════════════════════════

async def handle_innings_end(chat_id: int, bot: Bot):
    g = games[chat_id]
    inn = g["innings"]
    bt  = g["batting_team"]
    runs, wkts, balls = current_score(g)
    ov = f"{balls//6}.{balls%6}"

    reason = ""
    if overs_done(g):
        reason = f"All {g['max_overs']} overs bowled"
    elif wickets_done(g):
        reason = f"All out ({wkts} wickets)"
    elif target_chased(g):
        reason = "Target achieved!"

    await bot.send_message(
        chat_id,
        f"🏁 *Innings {inn} over!* — {reason}\n\n"
        f"{g['team_names'][bt]}: *{runs}/{wkts}* ({ov} ov)",
        parse_mode="Markdown"
    )

    if inn == 1:
        # swap teams for innings 2
        g["innings"]      = 2
        g["batting_team"] , g["bowling_team"] = g["bowling_team"], g["batting_team"]
        await start_innings(chat_id, bot)
    else:
        await declare_winner(chat_id, bot)


async def declare_winner(chat_id: int, bot: Bot):
    g = games[chat_id]
    g["phase"] = "ended"

    inn1_bat = g["bowling_team"]
    inn2_bat = g["batting_team"]

    inn1_runs = g["scores"][1][inn1_bat]
    inn2_runs = g["scores"][2][inn2_bat]

    if inn2_runs > inn1_runs:
        wkts_fallen = g["wickets"][2][inn2_bat]
        wkts_remain = max_wickets(g) - wkts_fallen
        winner_name = g["team_names"][inn2_bat]
        result_msg = (
            f"🏆 *{winner_name} wins!*\n\n"
            f"They chased {inn1_runs + 1} with {wkts_remain} wicket(s) remaining.\n\n"
        )
    elif inn1_runs > inn2_runs:
        diff = inn1_runs - inn2_runs
        winner_name = g["team_names"][inn1_bat]
        result_msg = (
            f"🏆 *{winner_name} wins!*\n\n"
            f"Won by {diff} run(s).\n\n"
        )
    else:
        result_msg = "🤝 *Match tied!*\n\n"

    final_msg = result_msg + scoreboard_text(g)
    await bot.send_message(chat_id, final_msg, parse_mode="Markdown")

    # ── Player of the Match ──────────────────────────────────────
    potm_data, potm_score = get_potm(g)
    if potm_data and potm_score > 0:
        potm_uid, potm_name = potm_data
        st = g["stats"].get(potm_uid, {})
        runs_scored = st.get("runs", 0)
        wkts_taken  = st.get("wickets", 0)
        balls_faced = st.get("balls_faced", 0)
        balls_bowled= st.get("balls_bowled", 0)

        potm_text = (
            f"🌟 *Player of the Match*\n\n"
            f"🏅 *{potm_name}*\n\n"
            f"🏏 Runs: *{runs_scored}* ({balls_faced} balls)\n"
            f"🎳 Wickets: *{wkts_taken}* ({balls_bowled} balls)\n\n"
            f"Outstanding performance! 👏"
        )

        # Profile photo fetch karne ki koshish karo
        try:
            photos = await bot.get_user_profile_photos(potm_uid, limit=1)
            if photos.total_count > 0:
                photo = photos.photos[0][-1]  # largest size
                await bot.send_photo(
                    chat_id,
                    photo=photo.file_id,
                    caption=potm_text,
                    parse_mode="Markdown"
                )
            else:
                await bot.send_message(chat_id, potm_text, parse_mode="Markdown")
        except Exception:
            await bot.send_message(chat_id, potm_text, parse_mode="Markdown")

    del games[chat_id]


# ═══════════════════════════════════════════════════════════════
#  /add  — reply karke kisi ko team mein add karo
# ═══════════════════════════════════════════════════════════════

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/add a  ya  /add b — host kisi ko bhi kisi team mein add kar sakta hai."""
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g:
        await update.message.reply_text("⚠️ Koi active match nahi hai.")
        return

    user_id = update.effective_user.id

    # Lobby mein koi bhi join kar sakta hai, game ke beech mein sirf host
    if g["phase"] != "lobby" and not is_host(g, user_id):
        host_name = g["host"][1] if g["host"] else "Host"
        await update.message.reply_text(f"❌ Game ke beech mein sirf host *{host_name}* players add kar sakta hai!", parse_mode="Markdown")
        return

    args = ctx.args
    if not args or args[0].upper() not in ["A", "B"]:
        await update.message.reply_text("📝 Usage: Kisi ke message ka reply karke `/add a` ya `/add b` likho.", parse_mode="Markdown")
        return

    team = args[0].upper()

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        target_user = update.effective_user

    uid      = target_user.id
    username = target_user.username or target_user.first_name
    other    = "B" if team == "A" else "A"

    if uid in g["joined"]:
        prev = g["joined"][uid]
        if prev == team:
            await update.message.reply_text(f"⚠️ *{username}* pehle se Team {team} mein hai!", parse_mode="Markdown")
            return
        g["teams"][prev] = [(u, n) for u, n in g["teams"][prev] if u != uid]

    g["joined"][uid] = team
    g["teams"][team].append((uid, username))

    if g["phase"] == "lobby":
        await update_lobby_message(g, ctx.bot)

    await update.message.reply_text(
        f"✅ *{username}* Team *{team}* ({g['team_names'][team]}) mein add ho gaya!",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════
#  /remove  — host kisi ko team se remove kar sakta hai
# ═══════════════════════════════════════════════════════════════

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/remove — host kisi ke message ka reply karke use team se nikalta hai."""
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g:
        await update.message.reply_text("⚠️ Koi active match nahi hai.")
        return

    user_id = update.effective_user.id
    if not is_host(g, user_id):
        host_name = g["host"][1] if g["host"] else "Host"
        await update.message.reply_text(f"❌ Sirf host *{host_name}* players remove kar sakta hai!", parse_mode="Markdown")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("📝 Jis player ko remove karna hai uske message ka reply karke `/remove` likho.", parse_mode="Markdown")
        return

    target_user = update.message.reply_to_message.from_user
    uid         = target_user.id
    username    = target_user.username or target_user.first_name

    if uid not in g["joined"]:
        await update.message.reply_text(f"⚠️ *{username}* kisi bhi team mein nahi hai.", parse_mode="Markdown")
        return

    team = g["joined"][uid]
    g["teams"][team] = [(u, n) for u, n in g["teams"][team] if u != uid]
    del g["joined"][uid]

    # Agar current batter/bowler/striker tha toh reset karo
    if g.get("batter") == uid:
        g["batter"] = None
    if g.get("non_striker") == uid:
        g["non_striker"] = None
    if g.get("bowler") == uid:
        g["bowler"] = None

    if g["phase"] == "lobby":
        await update_lobby_message(g, ctx.bot)

    await update.message.reply_text(
        f"🗑️ *{username}* ko Team *{team}* se remove kar diya gaya.",
        parse_mode="Markdown"
    )


async def cmd_changehost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/changehost — reply karke naya host set karo."""
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g:
        await update.message.reply_text("⚠️ Koi active match nahi hai.")
        return

    user_id = update.effective_user.id
    if not is_host(g, user_id):
        host_name = g["host"][1] if g["host"] else "Host"
        await update.message.reply_text(
            f"❌ Sirf current host *{host_name}* host change kar sakta hai!",
            parse_mode="Markdown"
        )
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "📝 Jisko host banana hai uske message ka reply karke `/changehost` likho.",
            parse_mode="Markdown"
        )
        return

    new_host_user = update.message.reply_to_message.from_user
    new_uid       = new_host_user.id
    new_uname     = new_host_user.username or new_host_user.first_name

    old_uname = g["host"][1]
    g["host"] = (new_uid, new_uname)

    await update.message.reply_text(
        f"👑 Host changed!\n\n"
        f"*{old_uname}* → *{new_uname}*\n\n"
        f"*{new_uname}* ab match ka host hai!",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════
#  /changeover  — host overs change kar sakta hai mid-game
# ═══════════════════════════════════════════════════════════════

async def cmd_changeover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/changeover 10 — host match ke beech mein overs change kar sakta hai."""
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g:
        await update.message.reply_text("⚠️ Koi active match nahi hai.")
        return

    user_id = update.effective_user.id
    if not is_host(g, user_id):
        host_name = g["host"][1] if g["host"] else "Host"
        await update.message.reply_text(
            f"❌ Sirf host *{host_name}* overs change kar sakta hai!",
            parse_mode="Markdown"
        )
        return

    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "📝 Usage: `/changeover <1-20>`\nExample: `/changeover 10`",
            parse_mode="Markdown"
        )
        return

    new_overs = int(args[0])
    if new_overs < 1 or new_overs > 20:
        await update.message.reply_text("❌ Overs 1 se 20 ke beech hone chahiye!")
        return

    old_overs     = g["max_overs"]
    g["max_overs"] = new_overs
    await update.message.reply_text(
        f"✅ Overs changed: *{old_overs}* → *{new_overs}*\n\n"
        f"👑 Host ne overs update kar diye!",
        parse_mode="Markdown"
    )


async def cmd_teams(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g:
        await update.message.reply_text("⚠️ Koi active match nahi hai.")
        return

    lines = ["👥 *Teams*\n━━━━━━━━━━━━━━━━━━━━"]

    for team in ["A", "B"]:
        name    = g["team_names"][team]
        players = g["teams"][team]
        cap     = g["captains"][team]
        cap_id  = cap[0] if cap else None

        lines.append(f"\n🔵 *{name}* ({len(players)} players)")
        if not players:
            lines.append("  — Koi nahi")
        else:
            for uid, uname in players:
                crown = " 👑" if uid == cap_id else ""
                lines.append(f"  • {uname}{crown}")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("👑 = Captain")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
#  /score
# ═══════════════════════════════════════════════════════════════

async def cmd_score(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g:
        await update.message.reply_text("⚠️ Koi active match nahi hai.")
        return
    await update.message.reply_text(scoreboard_text(g), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
#  /endmatch
# ═══════════════════════════════════════════════════════════════

async def cmd_endmatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if not g:
        await update.message.reply_text("⚠️ Koi active match nahi hai.")
        return

    user_id = update.effective_user.id
    if not is_host(g, user_id):
        host_name = g["host"][1] if g["host"] else "Host"
        await update.message.reply_text(
            f"❌ Sirf host *{host_name}* match end kar sakta hai!",
            parse_mode="Markdown"
        )
        return

    # Confirm/Cancel buttons
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm End", callback_data="endmatch_confirm"),
        InlineKeyboardButton("❌ Cancel",      callback_data="endmatch_cancel"),
    ]])
    await update.message.reply_text(
        "⚠️ *Kya aap match end karna chahte hain?*\n\n"
        "Confirm End dabane se match turant band ho jaayega.",
        reply_markup=kb, parse_mode="Markdown"
    )


async def cb_endmatch_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    g = get_game(chat_id)

    if not g:
        await query.edit_message_text("⚠️ Koi active match nahi hai.")
        return

    if not is_host(g, query.from_user.id):
        await query.answer("Sirf host confirm kar sakta hai!", show_alert=True)
        return

    sb = scoreboard_text(g) if g["phase"] not in ["lobby", "captain", "toss", "overs"] else ""
    if g["lobby_task"]:
        g["lobby_task"].cancel()
    if g.get("delivery_task") and not g["delivery_task"].done():
        g["delivery_task"].cancel()
    del games[chat_id]

    await query.edit_message_text(
        f"🛑 *Match ended by host.*\n\n{sb}",
        parse_mode="Markdown"
    )


async def cb_endmatch_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Match continue hai! ✅", show_alert=True)
    await query.edit_message_text("✅ Match continue hai — end cancel kar diya.")


# ═══════════════════════════════════════════════════════════════
#  WELCOME  —  /start  +  bot group mein add hone pe
# ═══════════════════════════════════════════════════════════════

WELCOME_TEXT = (
    "🏏 *Welcome to Cricket Dosti\\!*\n\n"
    "A fun\\-filled Cricket Game Bot\n"
    "*Play, Win & Have Fun Together\\!*\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📋 *Commands:*\n\n"
    "🏟️ /startmatch — Naya match shuru karo\n"
    "👥 /teams — Dono teams dekho\n"
    "📊 /score — Live scorecard dekho\n"
    "➕ /add a or b — Player ko team mein add karo\n"
    "❌ /remove — Player ko team se hatao\n"
    "🔄 /changeover — Overs change karo\n"
    "🏏 /select\\_batter — Striker choose karo\n"
    "🎳 /select\\_bowler — Bowler choose karo\n"
    "🛑 /endmatch — Match band karo \\(sirf host\\)\n"
    "❓ /help — Help dekho\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🎮 *Kaise khele:*\n\n"
    "1️⃣ Bowler 👉 Bowl Now button dabao → DM mein *1\\-6* type karo\n"
    "2️⃣ Batter 👉 Group mein *0\\-6* number type karo\n\n"
    "💡 Same number = *WICKET\\!*\n"
    "💡 Alag number = Batter ke runs\\!\n"
    "💡 1,3,5 runs = Strike change 🔄\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🎯 Type /startmatch to begin\\!"
)


async def send_welcome(chat_id: int, bot: Bot):
    """Send welcome image with caption."""
    # Hardcoded absolute path — Windows pe seedha kaam karega
    possible_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.png"),
        r"C:\Users\mantu\Downloads\CricketBot\welcome.png",
        "welcome.png",
    ]

    image_path = None
    for p in possible_paths:
        if os.path.exists(p):
            image_path = p
            logger.info(f"Welcome image found at: {p}")
            break

    if not image_path:
        logger.warning(f"welcome.png not found! Searched: {possible_paths}")

    try:
        if image_path:
            with open(image_path, "rb") as img:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=img,
                    caption=WELCOME_TEXT,
                    parse_mode="MarkdownV2"
                )
        else:
            await bot.send_message(chat_id, WELCOME_TEXT, parse_mode="MarkdownV2")
    except Exception as e:
        logger.warning(f"Welcome image send failed: {e}")
        await bot.send_message(chat_id, WELCOME_TEXT, parse_mode="MarkdownV2")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/start command — DM ya group dono mein kaam karta hai.
    /start bowl → bowler ke liye direct number prompt."""
    args = ctx.args
    user_id = update.effective_user.id

    # Deep link: t.me/bot?start=bowl — bowler ne button dabaya
    if args and args[0] == "bowl":
        # find active match where this user is bowler
        target_game = None
        for cid, g in games.items():
            if g["phase"] == "batting" and g["bowler"] == user_id:
                target_game = g
                break

        if not target_game:
            await update.message.reply_text(
                "⚠️ Tum kisi match mein active bowler nahi ho abhi.\n"
                "Pehle group mein match start karo!"
            )
            return

        if target_game["bowler_choice"] is not None:
            await update.message.reply_text("✅ Tumne pehle se bowl kar diya! Batter ka wait karo.")
            return

        bowler_name = next(
            u[1] for u in target_game["teams"][target_game["bowling_team"]]
            if u[0] == user_id
        )
        await update.message.reply_text(
            f"🎯 *Bowl karo, {bowler_name}!*\n\n"
            f"Apna number bhejo: `/bowl <1-6>`\n\n"
            f"Example: `/bowl 4`\n\n"
            f"💡 Batter ke number se match hua toh *WICKET!*",
            parse_mode="Markdown"
        )
        return

    # Normal /start — welcome image
    await send_welcome(update.effective_chat.id, ctx.bot)


async def on_new_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Jab bot kisi group mein add ho tab welcome bhejo."""
    for member in update.message.new_chat_members:
        if member.id == ctx.bot.id:
            await send_welcome(update.effective_chat.id, ctx.bot)
            break




async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏏 *Cricket Dosti — Commands*\n\n"
        "*/startmatch* — Naya match shuru karo\n"
        "*/add a* — Reply karke Team A mein add karo\n"
        "*/add b* — Reply karke Team B mein add karo\n"
        "*/teams* — Dono teams dekho\n"
        "*/score* — Live scorecard dekho\n"
        "*/select\\_batter* — Batting captain striker choose kare\n"
        "*/select\\_bowler* — Bowling captain bowler choose kare\n"
        "*/endmatch* — Match band karo\n\n"
        "🎮 *Kaise khele:*\n"
        "Bowler → Bot ka DM kholo → *1-6* type karo\n"
        "Batter → Group mein *0-6* type karo\n"
        "Same number = WICKET! | Alag = batter ke runs!\n"
        "1,3,5 runs = Strike change 🔄",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════
#  KEEP-ALIVE SERVER  — Render free tier ko sleep se rokta hai
# ═══════════════════════════════════════════════════════════════

class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Cricket Bot is alive!")

    def log_message(self, format, *args):
        pass  # server logs band karo


def run_keep_alive():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    logger.info(f"Keep-alive server running on port {port}")
    server.serve_forever()


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    # Keep-alive server alag thread mein chalao (Render ke liye)
    t = threading.Thread(target=run_keep_alive, daemon=True)
    t.start()

    # Bot token — env variable se lo (secure) ya hardcode karo
    token = os.environ.get("BOT_TOKEN", BOT_TOKEN)
    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("startmatch",     cmd_startmatch))
    app.add_handler(CommandHandler("add",            cmd_add))
    app.add_handler(CommandHandler("remove",         cmd_remove))
    app.add_handler(CommandHandler("changeover",     cmd_changeover))
    app.add_handler(CommandHandler("changehost",     cmd_changehost))
    app.add_handler(CommandHandler("select_batter",  cmd_select_batter))
    app.add_handler(CommandHandler("select_bowler",  cmd_select_bowler))
    app.add_handler(CommandHandler("bat",            cmd_bat))
    app.add_handler(CommandHandler("bowl",           cmd_bowl))
    app.add_handler(CommandHandler("score",          cmd_score))
    app.add_handler(CommandHandler("teams",          cmd_teams))
    app.add_handler(CommandHandler("endmatch",       cmd_endmatch))
    app.add_handler(CommandHandler("help",           cmd_help))

    # Plain number input
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"^\d+$"),
        handle_number_input
    ))

    # Bot group mein add hone pe welcome
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_member))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_join,               pattern=r"^join_[AB]$"))
    app.add_handler(CallbackQueryHandler(cb_force_start,        pattern=r"^force_start$"))
    app.add_handler(CallbackQueryHandler(cb_captain,            pattern=r"^cap_[AB]_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_toss,               pattern=r"^toss_(heads|tails)$"))
    app.add_handler(CallbackQueryHandler(cb_choose_innings,     pattern=r"^choose_(bat|bowl)$"))
    app.add_handler(CallbackQueryHandler(cb_overs,              pattern=r"^overs_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_select_batter,      pattern=r"^selbatter_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_select_non_striker, pattern=r"^selnonstriker_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_select_bowler,      pattern=r"^selbowler_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_endmatch_confirm,   pattern=r"^endmatch_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_endmatch_cancel,    pattern=r"^endmatch_cancel$"))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
