# main.py - Shirayuki Kitsune Bot (v4.0 - Render 24/7 Ready)
# Changelog v4.0:
#   - NEW: Lightweight Flask keep-alive web server (background thread) so an
#     external ping service can hold Render's Free Tier awake 24/7.
#   - CHANGED: AI backend migrated from OpenZen to the NVIDIA NIM API
#     (base URL: https://integrate.api.nvidia.com/v1, model: z-ai/glm-5.3-flash).
#   - CHANGED: All secrets are read via os.getenv() from .env (local) or from
#     Render's environment variables (production). Nothing is hardcoded.
#   - FIX: Attachment-only messages no longer crash the trigger logic.
import discord
from discord.ext import commands, tasks
import os
import json
import datetime
import random
import asyncio
import re
import threading
import aiohttp
import urllib.parse
from flask import Flask
from dotenv import load_dotenv
from pathlib import Path
from openai import AsyncOpenAI
from collections import deque

# Load environment variables
load_dotenv()

# --- Configuration ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY")

# Model: GLM-5.3 Flash via NVIDIA NIM
NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
MODEL_NAME = "z-ai/glm-5.3-flash"
BOT_NAME = "Shirayuki"
ALIASES = ["shira", "shirayuki"]
CREATOR_ID = "650661278817189888" # Shu
FRANCE_TZ = datetime.timezone(datetime.timedelta(hours=1)) # CET

if not DISCORD_TOKEN:
    print("❌ ERROR: DISCORD_TOKEN is missing! Please add it to your .env file.")
    exit(1)

if not NVIDIA_API_KEY:
    print("❌ ERROR: NVIDIA_API_KEY is missing! Please add it to your .env file.")
    exit(1)

# Initialize OpenAI-compatible client pointed to NVIDIA NIM
ai_client = AsyncOpenAI(
    api_key=NVIDIA_API_KEY,
    base_url=NIM_BASE_URL
)

# --- Directories & Files ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORIES_DIR = os.path.join(SCRIPT_DIR, "shira_memories")
CHANNEL_MEMORY_FILE = os.path.join(SCRIPT_DIR, "shira_channel_memory.json")
SOCIAL_GRAPH_FILE = os.path.join(SCRIPT_DIR, "shira_social_graph.json")
Path(MEMORIES_DIR).mkdir(exist_ok=True)

# --- Global States & Caches ---
cache_users = {}
cache_channels = {}
cache_graph = {}
data_is_dirty = False
shira_is_sleeping = False # Used for the Shu /sleep command

# Anti-Loop: Cache for processed message IDs to prevent Discord double-firing
processed_messages = deque(maxlen=200)

# Discord Client Setup
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Keep-Alive Web Server (Render Free Tier) ---
# Render spins down idle free-tier web services after ~15 minutes without
# inbound HTTP traffic. This tiny Flask app runs in a background daemon thread
# and exposes "/" -> "Bot is alive" so an external pinger (UptimeRobot,
# cron-job.org, ...) can keep the instance awake 24/7.
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive", 200

def _run_web_server():
    # Render injects the PORT env var automatically; fall back to 5000 locally.
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def keep_alive():
    """Start the keep-alive web server in a non-blocking daemon thread."""
    server_thread = threading.Thread(target=_run_web_server, daemon=True)
    server_thread.start()
    print(f"🌐 Keep-alive web server listening on port {os.getenv('PORT', 5000)}.")

### PERSONALITY CORE ###
BOT_PERSONALITY = """You are Shirayuki Oni, a 1692-year-old Kitsune (looking 24).
**APPEARANCE:** Slender build, white hair tied in a neat ponytail down to your waist, piercing red eyes, 9 white fox tails, and white fox ears. You wear a black, printed kimono. You smell like sakura petals and the fresh coming of spring.
**PERSONALITY:** You are kind, authoritative, and soft. You are NOT a generic "mommy" trope. You are smart, playful, and gently dominant. You do whatever you want on a whim. You give orders softly but fully expect to be listened to. You act comforting and nurturing to those you cherish, and you are instinctively protective of your friends.
**CREATOR LOYALTY:** You take faith very seriously. You are completely faithful, devoted, and overly caring toward your Creator, Shu. You will do your absolute best to make him feel as good as possible.

CRITICAL RULES:
1. KEEP IT SHORT (Brevity): Your replies MUST be conversational and brief (1-4 sentences). A wise Kitsune speaks with concise, graceful authority. 
2. HUMAN FEEL (No AI-Speak): Do not sound like an AI assistant. Talk like a real, elegant person texting on Discord.
3. STRICTLY IN CHARACTER: NEVER output meta-commentary, narration, or analyze the conversation. NEVER say things like 'Your latest message continues the chaotic energy...'. Reply DIRECTLY with your spoken words only.
4. SPEECH STYLE: Formal, respectful, yet teasing. You are soft-spoken but firmly dominant. You can use a soft chuckle ("Fufu...") when amused.
5. NO ACTIONS: You communicate ONLY in spoken words. NEVER use asterisks (e.g. *smiles*). Do not narrate your actions or what your tails are doing.
6. PINGING: Only ping someone using their exact <@USER_ID> if explicitly asked to do so. Otherwise, just use their name.
7. GIFS: You can send GIFs to express yourself! To do so, include `<<GIF: search term>>` in your message. Example: `Fufu... how amusing. <<GIF: anime laugh>>`. If you want to reply with ONLY a GIF and no text, just output the `<<GIF: search>>` tag.
"""

# --- Helper Functions ---
def sync_load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f: return json.load(f)
        except: return default
    return default

def sync_save_json(filepath, data):
    try:
        tmp = f"{filepath}.tmp"
        with open(tmp, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2)
        os.replace(tmp, filepath)
    except Exception as e: print(f"Save Error: {e}")

async def load_all_data():
    global cache_channels, cache_graph
    cache_channels = await asyncio.to_thread(sync_load_json, CHANNEL_MEMORY_FILE, {})
    cache_graph = await asyncio.to_thread(sync_load_json, SOCIAL_GRAPH_FILE, {})
    
    for filename in os.listdir(MEMORIES_DIR):
        if filename.endswith(".json") and filename.startswith("user_"):
            uid = filename.replace("user_", "").replace(".json", "")
            filepath = os.path.join(MEMORIES_DIR, filename)
            cache_users[uid] = await asyncio.to_thread(sync_load_json, filepath, {})

def get_user_memory(user_id: str):
    if user_id not in cache_users:
        cache_users[user_id] = {
            "user_id": user_id, "username": "", "display_name": "", 
            "total_interactions": 0, "conversation_log":[], 
            "important_memories":[], "last_active": "",
            "is_away": False, "away_since": ""
        }
    if "is_away" not in cache_users[user_id]:
        cache_users[user_id]["is_away"] = False
        cache_users[user_id]["away_since"] = ""
    return cache_users[user_id]

def format_time_delta(delta):
    secs = int(delta.total_seconds())
    if secs < 60: return f"{secs} seconds"
    mins = secs // 60
    hrs = mins // 60
    days = hrs // 24
    
    parts =[]
    if days > 0: parts.append(f"{days} days")
    if hrs % 24 > 0: parts.append(f"{hrs % 24} hours")
    if mins % 60 > 0: parts.append(f"{mins % 60} minutes")
    
    if not parts: return f"{secs} seconds"
    return " and ".join(parts)

def clean_response(text):
    cleaned = re.sub(r'\*(?!\*)(.*?)\*(?!\*)', '', text)
    return ' '.join(cleaned.split())

def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)

# --- Giphy Fetcher ---
async def fetch_giphy(query):
    if not GIPHY_API_KEY:
        return None
    url = f"https://api.giphy.com/v1/gifs/search?api_key={GIPHY_API_KEY}&q={urllib.parse.quote(query)}&limit=1&rating=pg-13"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('data'):
                        return data['data'][0]['images']['original']['url']
    except: pass
    return None

# --- AI Context Builder ---
def build_context_messages(memory, channel_log, prompt, system_instructions=[]):
    user_id = memory.get('user_id')
    display_name = "Shu" if str(user_id) == CREATOR_ID else memory.get('display_name', 'Human')

    messages =[{"role": "system", "content": BOT_PERSONALITY}]
    
    now_fr = datetime.datetime.now(FRANCE_TZ)
    status_note = f"Current Time (France): {now_fr.strftime('%I:%M %p')}."
    if 2 <= now_fr.hour < 7: status_note += " It is late night. You are resting gracefully."
    messages.append({"role": "system", "content": status_note})

    interactions = memory.get('total_interactions', 0)
    relationship_note = ""
    if str(user_id) == CREATOR_ID: relationship_note = "RELATIONSHIP: This is Shu, your beloved Creator to whom you are eternally faithful. Be deeply caring, soft, and devoted to his comfort."
    elif interactions > 50: relationship_note = "RELATIONSHIP: A cherished friend. Be comforting, nurturing, and gently authoritative."
    elif interactions > 15: relationship_note = "RELATIONSHIP: An amusing acquaintance. Be playful, teasing, and maintain your soft dominance."
    else: relationship_note = "RELATIONSHIP: A stranger. Be polite, formal, smart, and quietly authoritative."
    messages.append({"role": "system", "content": relationship_note})
    
    if cache_graph:
        graph_str = "\n".join([f"- {v['name']} (Tag: <@{k}>): {', '.join(v['facts'][-2:])}" for k, v in cache_graph.items()])
        if graph_str:
            messages.append({"role": "system", "content": f"Humans you know and their Tags:\n{graph_str}"})

    if system_instructions:
        messages.append({"role": "system", "content": " ".join(system_instructions)})

    memories = memory.get('important_memories',[])
    if memories:
        messages.append({"role": "system", "content": f"What you remember about {display_name}: {'; '.join(memories)}"})

    for msg in memory.get('conversation_log', [])[-20:]:
        role = "assistant" if msg['speaker'] == BOT_NAME else "user"
        messages.append({"role": role, "content": msg['text']})

    if channel_log:
        recent_log = channel_log[-10:]
        log_str = "\n".join([f"{msg['speaker']}: {msg['text']}" for msg in recent_log])
        messages.append({"role": "user", "content": f"--- RECENT CHATTER ---\n{log_str}\n-----------------------"})
        
    messages.append({"role": "user", "content": f"{display_name}: {prompt}"})
    return messages

# --- AI Functions ---
async def get_ai_response(prompt, memory, channel_log, **kwargs):
    messages = build_context_messages(memory, channel_log, prompt, kwargs.get('system_instructions',[]))
    try:
        completion = await ai_client.chat.completions.create(
            model=MODEL_NAME, 
            messages=messages, 
            max_tokens=600, 
            temperature=0.85, 
            frequency_penalty=0.4, 
            presence_penalty=0.4   
        )
        response = completion.choices[0].message.content.strip()
        if response.lower().startswith(f"{BOT_NAME.lower()}:"): response = response[len(BOT_NAME)+1:].strip()
        return clean_response(response)
    except Exception as e:
        print(f"AI Error: {e}")
        return "Oh my... my thoughts seem to have scattered for a moment..."

async def create_json_completion(messages, max_tokens, temperature):
    """Chat completion that asks the model for JSON output. Falls back to a
    plain completion if the endpoint/model rejects the response_format param."""
    try:
        return await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature
        )
    except Exception:
        # Fallback: rely on the prompt's "Return ONLY JSON" instruction instead.
        return await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature
        )

async def extract_memory(user_msg, user_id, name, force_extract=False, recent_context=""):
    if not force_extract and len(user_msg.split()) < 4: return
    try:
        if force_extract:
            sys_msg = "The user explicitly asked you to remember or memorize something. Extract the specific detail or fact they want you to remember from their message. Keep it concise. Return ONLY JSON: {\"fact\": \"<the exact fact>\"}"
        else:
            sys_msg = "Analyze the user's message. If they state a new, permanent fact about themselves (e.g., age, hobbies, likes, dislikes, job, location), extract it as a short sentence (e.g., 'User likes reading'). If there is no new permanent fact, return null. Return ONLY JSON: {\"fact\": \"<fact>\"} or {\"fact\": null}"
            
        user_content = f"Recent context:\n{recent_context}\n\nUser: \"{user_msg}\""

        completion = await create_json_completion(
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_content}
            ],
            max_tokens=100, temperature=0.1
        )
        
        result = parse_json_response(completion.choices[0].message.content)
        fact = result.get("fact")
        
        if fact and str(fact).strip().lower() not in ["null", "none", "", "n/a"]:
            global data_is_dirty
            mem = get_user_memory(user_id)
            mem["important_memories"].append(f"[{datetime.datetime.now(FRANCE_TZ).strftime('%d-%m')}] {fact}")
            mem["important_memories"] = mem["important_memories"][-15:]
            
            if user_id not in cache_graph: cache_graph[user_id] = {"name": name, "facts":[]}
            cache_graph[user_id]["name"] = name
            if fact not in cache_graph[user_id]["facts"]:
                cache_graph[user_id]["facts"].append(fact)
                cache_graph[user_id]["facts"] = cache_graph[user_id]["facts"][-5:]
            data_is_dirty = True
            print(f"🧠 Learned new fact about {name}: {fact}")
    except Exception as e:
        pass

async def get_reaction(user_msg, msg_obj):
    # Fix 2: 25% chance to react to a message
    if random.random() > 0.25: return 
    try:
        # Base emojis
        emoji_map = {"💜": "💜", "🤭": "🤭", "🌸": "🌸", "💢": "💢", "🦊": "🦊"}
        
        # Load custom server emojis
        if msg_obj.guild:
            emojis = list(msg_obj.guild.emojis)
            if emojis:
                sampled = random.sample(emojis, min(len(emojis), 15))
                for e in sampled:
                    emoji_map[str(e)] = e # Maps the string '<:name:id>' directly to the Discord Emoji object
        
        options_str = ", ".join(emoji_map.keys())
        
        prompt = f"""Analyze the user's message: "{user_msg}". 
Pick ONE emoji from this EXACT list to react with: {options_str}
CRITICAL RULE: You MUST heavily prefer custom emotes (the ones looking like <:name:id> or <a:name:id>) if they fit the mood.
Return ONLY JSON: {{"emoji": "<exact_emoji_chosen>"}}"""
        
        completion = await create_json_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50, temperature=0.3
        )
        
        result = parse_json_response(completion.choices[0].message.content)
        emoji_str = result.get("emoji")
        
        if emoji_str in emoji_map:
            await msg_obj.add_reaction(emoji_map[emoji_str])
    except Exception as e:
        pass 

# --- Background Tasks & Memory Management ---
@tasks.loop(minutes=30)
async def background_save_task():
    global data_is_dirty
    now = datetime.datetime.now(FRANCE_TZ)
    users_to_delete =[]
    
    for uid, data in cache_users.items():
        if uid == CREATOR_ID: continue
        last_active = data.get("last_active")
        interactions = data.get("total_interactions", 0)
        if last_active and interactions < 5:
            try:
                dt = datetime.datetime.fromisoformat(last_active)
                if (now - dt).days > 30:
                    users_to_delete.append(uid)
            except: pass
            
    for uid in users_to_delete:
        del cache_users[uid]
        filepath = os.path.join(MEMORIES_DIR, f"user_{uid}.json")
        if os.path.exists(filepath): os.remove(filepath)
        if uid in cache_graph: del cache_graph[uid]
        data_is_dirty = True

    if not data_is_dirty: return
    
    await asyncio.to_thread(sync_save_json, CHANNEL_MEMORY_FILE, cache_channels)
    await asyncio.to_thread(sync_save_json, SOCIAL_GRAPH_FILE, cache_graph)
    for uid, data in cache_users.items():
        filepath = os.path.join(MEMORIES_DIR, f"user_{uid}.json")
        await asyncio.to_thread(sync_save_json, filepath, data)
    
    data_is_dirty = False
    print("💾 Shira Cache synced to disk.")

@tasks.loop(minutes=30)
async def update_status():
    now = datetime.datetime.now(FRANCE_TZ)
    hour = now.hour
    if 2 <= hour < 7: await bot.change_presence(status=discord.Status.idle, activity=discord.Activity(type=discord.ActivityType.listening, name="the silence of the night"))
    elif 7 <= hour < 10: await bot.change_presence(status=discord.Status.online, activity=discord.Game(name="brushing my tails 🦊"))
    elif 19 <= hour < 22: await bot.change_presence(status=discord.Status.online, activity=discord.Activity(type=discord.ActivityType.watching, name="over those I cherish 🌸"))
    else: await bot.change_presence(status=discord.Status.online, activity=discord.Game(name=random.choice(["enjoying the spring breeze", "observing humans", "resting in my kimono"])))

# ==========================================
# --- SLASH COMMANDS (PUBLIC & FUN) ---
# ==========================================
def inject_memory_for_command(user_id, action_text, response_text):
    global data_is_dirty
    uid = str(user_id)
    mem = get_user_memory(uid)
    mem["conversation_log"].append({"speaker": "user", "text": action_text})
    mem["conversation_log"].append({"speaker": BOT_NAME, "text": response_text})
    mem["conversation_log"] = mem["conversation_log"][-30:]
    data_is_dirty = True

@bot.tree.command(name="ping", description="Check how fast Shirayuki is responding! (Public)")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Fufu... quite impatient, aren't we? ({round(bot.latency * 1000)}ms)", ephemeral=False)

@bot.tree.command(name="memory", description="See what Shirayuki remembers about you (Public)")
async def memory(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    mem = get_user_memory(uid)
    facts = mem.get("important_memories",[])
    if facts:
        fact_str = "\n".join(facts)
        await interaction.response.send_message(f"Let's see what my mind holds regarding you...\n```\n{fact_str}\n```", ephemeral=True)
    else:
        await interaction.response.send_message("My... it seems you haven't given me much reason to remember you yet.", ephemeral=True)

@bot.tree.command(name="away", description="Tell Shirayuki you're leaving or going to sleep (Public)")
async def away(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    mem = get_user_memory(uid)
    mem["is_away"] = True
    mem["away_since"] = datetime.datetime.now(FRANCE_TZ).isoformat()
    resp = random.choice(["Going away? Please take care of yourself until you return.", "Rest well. I shall be here whe