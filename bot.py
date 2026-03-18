import traceback, sys
sys.excepthook = lambda *args: traceback.print_exception(*args)

import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
import wavelink
import config
import asyncio
import random
from collections import deque

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)

# ── Per-guild state ──────────────────────────────────────────────────────────
stay_247      = set()
autoplay      = set()
loop_modes    = {}
queues        = {}
history       = {}
dj_roles      = {}
now_msgs      = {}
active_filter = {}   # guild_id -> filter name string

AUTOPLAY_SEEDS = [
    "top hits 2024", "popular songs", "trending music",
    "best pop songs", "top rap hits", "popular edm",
    "viral tiktok songs", "best rock songs", "lofi chill beats"
]

# ── Filter presets (wavelink 3.x compatible) ─────────────────────────────
FILTER_PRESETS = {
    "nightcore":  {"type": "timescale", "speed": 1.3,  "pitch": 1.3,  "rate": 1.0},
    "speedup":    {"type": "timescale", "speed": 1.5,  "pitch": 1.0,  "rate": 1.0},
    "slowed":     {"type": "timescale", "speed": 0.75, "pitch": 0.9,  "rate": 1.0},
    "vaporwave":  {"type": "timescale", "speed": 0.8,  "pitch": 0.8,  "rate": 1.0},
    "bassboost":  {"type": "equalizer", "bands": [{"band": 0, "gain": 0.3}, {"band": 1, "gain": 0.3}, {"band": 2, "gain": 0.2}, {"band": 3, "gain": 0.1}, {"band": 4, "gain": 0.05}]},
    "8d":         {"type": "rotation",  "rotation_hz": 0.2},
    "earrape":    {"type": "equalizer", "bands": [{"band": i, "gain": 0.25} for i in range(15)]},
    "flat":       {"type": "flat"},
}
# ── Helpers ──────────────────────────────────────────────────────────────────

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = deque()
    return queues[guild_id]

def get_history(guild_id):
    if guild_id not in history:
        history[guild_id] = []
    return history[guild_id]

def has_dj(interaction: discord.Interaction):
    role_id = dj_roles.get(interaction.guild.id)
    if role_id is None:
        return True
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.id == role_id for r in interaction.user.roles)

def fmt_duration(ms):
    if not ms:
        return "Live"
    s = int(ms / 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

def progress_bar(position_ms, length_ms, size=20):
    if not length_ms:
        return "▬" * size
    filled = int((position_ms / length_ms) * size)
    filled = max(0, min(filled, size - 1))
    return "▬" * filled + "🔘" + "▬" * (size - filled - 1)

async def apply_filter(player: wavelink.Player, preset_name: str):
    """Apply a filter preset to the player (wavelink 3.x)."""
    preset  = FILTER_PRESETS.get(preset_name, {"type": "flat"})
    filters = wavelink.Filters()
    ptype   = preset.get("type", "flat")

    if ptype == "timescale":
        filters.timescale.set(
            speed=preset.get("speed", 1.0),
            pitch=preset.get("pitch", 1.0),
            rate=preset.get("rate",  1.0)
        )
    elif ptype == "equalizer":
        eq_bands = [wavelink.EQBand(band=b["band"], gain=b["gain"]) for b in preset.get("bands", [])]
        filters.equalizer.set(bands=eq_bands)
    elif ptype == "rotation":
        filters.rotation.set(rotation_hz=preset.get("rotation_hz", 0.2))
    # flat = empty Filters() = reset

    await player.set_filters(filters)

async def fetch_autoplay_track(current_track, guild_id):
    hist_titles = [t.title for t in get_history(guild_id)]
    queries = []
    if current_track:
        if current_track.author:
            queries.append(f"{current_track.author} songs")
        if current_track.title:
            queries.append(" ".join(current_track.title.split()[:3]) + " similar")
    queries.append(random.choice(AUTOPLAY_SEEDS))

    for query in queries:
        try:
            results = await wavelink.Playable.search(query)
            if results:
                fresh = [t for t in results if t.title not in hist_titles]
                pool  = fresh[:5] if fresh else results[:5]
                if pool:
                    return random.choice(pool)
        except Exception as e:
            print(f"Autoplay search error: {e}")
            continue
    return None

def build_now_playing_embed(player: wavelink.Player, track: wavelink.Playable, guild_id):
    q      = get_queue(guild_id)
    loop   = loop_modes.get(guild_id, "off")
    ap     = "✅" if guild_id in autoplay else "❌"
    filt   = active_filter.get(guild_id, "none")

    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**{track.title}**",
        color=0x5865F2
    )
    embed.add_field(name="🎤 Artist",    value=track.author or "Unknown", inline=True)
    embed.add_field(name="⏱ Duration",  value=fmt_duration(track.length), inline=True)
    embed.add_field(name="🔊 Volume",   value=f"{player.volume}%", inline=True)
    embed.add_field(name="🔁 Loop",     value=loop.capitalize(), inline=True)
    embed.add_field(name="🤖 Autoplay", value=ap, inline=True)
    embed.add_field(name="🎛️ Filter",   value=filt.capitalize(), inline=True)
    embed.add_field(name="📋 Queue",    value=f"{len(q)} track(s)", inline=True)

    bar = progress_bar(player.position, track.length)
    pos = fmt_duration(player.position)
    dur = fmt_duration(track.length)
    embed.add_field(
        name="Progress",
        value=f"`{pos}` {bar} `{dur}`",
        inline=False
    )

    if q:
        embed.add_field(
            name="⏭ Up Next",
            value="\n".join(f"`{i+1}.` {t.title}" for i, t in enumerate(list(q)[:5])),
            inline=False
        )

    if track.artwork:
        embed.set_thumbnail(url=track.artwork)

    embed.set_footer(text="Use buttons below to control playback")
    return embed

# ── Player Controls UI ───────────────────────────────────────────────────────

class PlayerControls(View):
    def __init__(self, player: wavelink.Player, guild_id):
        super().__init__(timeout=None)
        self.player   = player
        self.guild_id = guild_id

    @discord.ui.button(label="⏯", style=discord.ButtonStyle.success, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: Button):
        await self.player.pause(not self.player.paused)
        state = "⏸ Paused" if self.player.paused else "▶️ Resumed"
        await interaction.response.send_message(state, ephemeral=True)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await self.player.stop()
        await interaction.response.send_message("⏭ Skipped", ephemeral=True)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: Button):
        get_queue(self.guild_id).clear()
        stay_247.discard(self.guild_id)
        await self.player.disconnect()
        await interaction.response.send_message("⏹ Stopped", ephemeral=True)

    @discord.ui.button(label="🔉 -10%", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: Button):
        vol = max(self.player.volume - 10, 0)
        await self.player.set_volume(vol)
        await interaction.response.send_message(f"🔉 Volume → {vol}%", ephemeral=True)

    @discord.ui.button(label="🔊 +10%", style=discord.ButtonStyle.primary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: Button):
        vol = min(self.player.volume + 10, 200)
        await self.player.set_volume(vol)
        await interaction.response.send_message(f"🔊 Volume → {vol}%", ephemeral=True)

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.secondary, row=1)
    async def loop_btn(self, interaction: discord.Interaction, button: Button):
        modes        = ["off", "track", "queue"]
        current_mode = loop_modes.get(self.guild_id, "off")
        nxt          = modes[(modes.index(current_mode) + 1) % len(modes)]
        loop_modes[self.guild_id] = nxt
        labels = {"off": "🔁 Loop OFF", "track": "🔂 Loop TRACK", "queue": "🔁 Loop QUEUE"}
        await interaction.response.send_message(labels[nxt], ephemeral=True)

    @discord.ui.button(label="🤖 Autoplay", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_btn(self, interaction: discord.Interaction, button: Button):
        gid = self.guild_id
        if gid in autoplay:
            autoplay.discard(gid)
            await interaction.response.send_message("🤖 Autoplay **OFF**", ephemeral=True)
        else:
            autoplay.add(gid)
            await interaction.response.send_message("🤖 Autoplay **ON**", ephemeral=True)

    @discord.ui.button(label="🎛️ Reset Filter", style=discord.ButtonStyle.secondary, row=2)
    async def reset_filter(self, interaction: discord.Interaction, button: Button):
        await self.player.set_filters(wavelink.Filters())
        active_filter[self.guild_id] = "none"
        await interaction.response.send_message("🎛️ Filter reset to **flat**", ephemeral=True)

# ── READY ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

    try:
        secure = getattr(config, 'LAVALINK_SECURE', False)
        node = wavelink.Node(
            uri=f"{'https' if secure else 'http'}://{config.LAVALINK_HOST}:{config.LAVALINK_PORT}",
            password=config.LAVALINK_PASSWORD,
            retries=10
        )
        await wavelink.Pool.connect(nodes=[node], client=bot)
        print("✅ Lavalink connected!")
    except Exception as e:
        print(f"❌ Lavalink connection failed: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands!")
    except Exception as e:
        print(f"❌ Slash sync failed: {e}")

    for guild in bot.guilds:
        autoplay.add(guild.id)

# ── TRACK END EVENT ──────────────────────────────────────────────────────────

@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    track  = payload.track
    gid    = player.guild.id
    q      = get_queue(gid)
    hist   = get_history(gid)
    loop   = loop_modes.get(gid, "off")

    autoplay.add(gid)

    if track:
        hist.append(track)
        if len(hist) > 20:
            hist.pop(0)

    if loop == "track" and track:
        await player.play(track)
        await _update_now_playing(player, track, gid)
        return

    if loop == "queue" and track:
        q.append(track)

    if q:
        next_track = q.popleft()
        await player.play(next_track)
        await _update_now_playing(player, next_track, gid)
        return

    for attempt in range(5):
        next_track = await fetch_autoplay_track(track, gid)
        if next_track:
            await player.play(next_track)
            await _update_now_playing(player, next_track, gid)
            return
        print(f"Autoplay attempt {attempt+1} failed, retrying...")
        await asyncio.sleep(1)

    results = await wavelink.Playable.search(random.choice(AUTOPLAY_SEEDS))
    if results:
        await player.play(results[0])
        await _update_now_playing(player, results[0], gid)
        return

    if gid not in stay_247:
        await asyncio.sleep(30)
        if not player.playing:
            await player.disconnect()

async def _update_now_playing(player, track, guild_id):
    msg = now_msgs.get(guild_id)
    if msg:
        try:
            embed = build_now_playing_embed(player, track, guild_id)
            await msg.edit(embed=embed, view=PlayerControls(player, guild_id))
        except Exception:
            pass

# ── VOICE STATE ──────────────────────────────────────────────────────────────

@bot.event
async def on_voice_state_update(member, before, after):
    if member == bot.user:
        return
    vc = member.guild.voice_client
    if not vc:
        return
    gid = member.guild.id
    if len(vc.channel.members) == 1:
        await asyncio.sleep(300)
        if (vc.is_connected()
                and len(vc.channel.members) == 1
                and not vc.is_playing()
                and gid not in stay_247
                and gid not in autoplay):
            await vc.disconnect()

# ── SLASH COMMANDS ───────────────────────────────────────────────────────────

@bot.tree.command(name="play", description="Play a song or add to queue")
@app_commands.describe(query="Song name or YouTube URL")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Join a voice channel first!", ephemeral=True)

    await interaction.response.defer()

    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        player = await interaction.user.voice.channel.connect(cls=wavelink.Player, self_deaf=True)

    autoplay.add(interaction.guild.id)

    tracks = await wavelink.Playable.search(query)
    if not tracks:
        return await interaction.followup.send("❌ No results found.")

    track = tracks[0]
    q     = get_queue(interaction.guild.id)

    if player.playing or player.paused:
        q.append(track)
        return await interaction.followup.send(f"📋 Queued: **{track.title}** (#{len(q)})")

    await player.play(track)
    embed = build_now_playing_embed(player, track, interaction.guild.id)
    msg   = await interaction.followup.send(embed=embed, view=PlayerControls(player, interaction.guild.id))
    now_msgs[interaction.guild.id] = msg

@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    if not has_dj(interaction):
        return await interaction.response.send_message("❌ You need the DJ role.", ephemeral=True)
    player: wavelink.Player = interaction.guild.voice_client
    if player and player.playing:
        await player.stop()
        await interaction.response.send_message("⏭ Skipped!")
    else:
        await interaction.response.send_message("❌ Nothing playing.", ephemeral=True)

@bot.tree.command(name="stop", description="Stop music and disconnect")
async def stop(interaction: discord.Interaction):
    if not has_dj(interaction):
        return await interaction.response.send_message("❌ You need the DJ role.", ephemeral=True)
    get_queue(interaction.guild.id).clear()
    stay_247.discard(interaction.guild.id)
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        await player.disconnect()
    await interaction.response.send_message("⏹ Stopped.")

@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and player.playing:
        await player.pause(True)
        await interaction.response.send_message("⏸ Paused.")
    else:
        await interaction.response.send_message("❌ Nothing playing.", ephemeral=True)

@bot.tree.command(name="resume", description="Resume the paused song")
async def resume(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and player.paused:
        await player.pause(False)
        await interaction.response.send_message("▶️ Resumed.")
    else:
        await interaction.response.send_message("❌ Nothing paused.", ephemeral=True)

@bot.tree.command(name="volume", description="Set volume (0-200)")
@app_commands.describe(vol="Volume level between 0 and 200")
async def volume(interaction: discord.Interaction, vol: int):
    if not has_dj(interaction):
        return await interaction.response.send_message("❌ You need the DJ role.", ephemeral=True)
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        return await interaction.response.send_message("❌ Nothing playing.", ephemeral=True)
    vol = max(0, min(200, vol))
    await player.set_volume(vol)
    await interaction.response.send_message(f"🔊 Volume → **{vol}%**")

@bot.tree.command(name="filter", description="Apply a music filter/preset")
@app_commands.describe(preset="Choose a filter preset")
@app_commands.choices(preset=[
    app_commands.Choice(name="🌙 Nightcore  (faster + higher pitch)", value="nightcore"),
    app_commands.Choice(name="⚡ Speed Up   (faster, same pitch)",    value="speedup"),
    app_commands.Choice(name="🌊 Slowed     (slower + lower pitch)",  value="slowed"),
    app_commands.Choice(name="🌸 Vaporwave  (dreamy slow)",           value="vaporwave"),
    app_commands.Choice(name="💥 Bass Boost (heavy bass)",            value="bassboost"),
    app_commands.Choice(name="🎧 8D Audio   (rotating audio)",        value="8d"),
    app_commands.Choice(name="💀 Earrape    (extreme boost)",         value="earrape"),
    app_commands.Choice(name="✅ Flat       (reset all filters)",     value="flat"),
])
async def filter_cmd(interaction: discord.Interaction, preset: str):
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        return await interaction.response.send_message("❌ Bot not in VC.", ephemeral=True)

    try:
        await apply_filter(player, preset)
        active_filter[interaction.guild.id] = preset if preset != "flat" else "none"

        labels = {
            "nightcore": "🌙 **Nightcore** applied! (faster + higher pitch)",
            "speedup":   "⚡ **Speed Up** applied! (1.5x speed)",
            "slowed":    "🌊 **Slowed** applied! (dreamy slow)",
            "vaporwave": "🌸 **Vaporwave** applied! (very slow + low pitch)",
            "bassboost": "💥 **Bass Boost** applied! (heavy bass)",
            "8d":        "🎧 **8D Audio** applied! (rotating sound)",
            "earrape":   "💀 **Earrape** applied! (extreme boost)",
            "flat":      "✅ **Filters reset** to flat!",
        }
        await interaction.response.send_message(labels.get(preset, "✅ Filter applied!"))
    except Exception as e:
        await interaction.response.send_message(f"❌ Filter failed: `{e}`", ephemeral=True)

@bot.tree.command(name="queue", description="Show the current queue")
async def queue(interaction: discord.Interaction):
    q = get_queue(interaction.guild.id)
    if not q:
        return await interaction.response.send_message("📋 Queue is empty.")
    lines = [f"`{i+1}.` {t.title} — {fmt_duration(t.length)}"
             for i, t in enumerate(list(q)[:15])]
    embed = discord.Embed(title="📋 Queue", description="\n".join(lines), color=0x5865F2)
    if len(q) > 15:
        embed.set_footer(text=f"...and {len(q)-15} more")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    if not has_dj(interaction):
        return await interaction.response.send_message("❌ You need the DJ role.", ephemeral=True)
    q = get_queue(interaction.guild.id)
    if not q:
        return await interaction.response.send_message("❌ Queue is empty.", ephemeral=True)
    lst = list(q)
    random.shuffle(lst)
    queues[interaction.guild.id] = deque(lst)
    await interaction.response.send_message("🔀 Queue shuffled!")

@bot.tree.command(name="remove", description="Remove a track from the queue")
@app_commands.describe(position="Position number in the queue")
async def remove(interaction: discord.Interaction, position: int):
    q = get_queue(interaction.guild.id)
    if position < 1 or position > len(q):
        return await interaction.response.send_message("❌ Invalid position.", ephemeral=True)
    lst = list(q)
    removed = lst.pop(position - 1)
    queues[interaction.guild.id] = deque(lst)
    await interaction.response.send_message(f"🗑️ Removed **{removed.title}**")

@bot.tree.command(name="clear", description="Clear the entire queue")
async def clear(interaction: discord.Interaction):
    if not has_dj(interaction):
        return await interaction.response.send_message("❌ You need the DJ role.", ephemeral=True)
    get_queue(interaction.guild.id).clear()
    await interaction.response.send_message("🗑️ Queue cleared.")

@bot.tree.command(name="loop", description="Set loop mode")
@app_commands.choices(mode=[
    app_commands.Choice(name="Off",   value="off"),
    app_commands.Choice(name="Track", value="track"),
    app_commands.Choice(name="Queue", value="queue"),
])
async def loop(interaction: discord.Interaction, mode: str = None):
    modes = ["off", "track", "queue"]
    gid   = interaction.guild.id
    if mode and mode in modes:
        loop_modes[gid] = mode
    else:
        cur = loop_modes.get(gid, "off")
        loop_modes[gid] = modes[(modes.index(cur) + 1) % len(modes)]
    labels = {"off": "🔁 Loop **OFF**", "track": "🔂 Loop **TRACK**", "queue": "🔁 Loop **QUEUE**"}
    await interaction.response.send_message(labels[loop_modes[gid]])

@bot.tree.command(name="autoplay", description="Toggle autoplay on/off")
async def autoplay_cmd(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid in autoplay:
        autoplay.discard(gid)
        await interaction.response.send_message("🤖 Autoplay **disabled**.")
    else:
        autoplay.add(gid)
        await interaction.response.send_message("🤖 Autoplay **enabled**!")

@bot.tree.command(name="247", description="Toggle 24/7 mode — bot stays in VC")
async def mode_247(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid in stay_247:
        stay_247.remove(gid)
        await interaction.response.send_message("❌ 24/7 mode disabled.")
    else:
        stay_247.add(gid)
        await interaction.response.send_message("✅ 24/7 mode enabled!")

@bot.tree.command(name="nowplaying", description="Show the current playing song")
async def nowplaying(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        return await interaction.response.send_message("❌ Nothing playing.", ephemeral=True)
    embed = build_now_playing_embed(player, player.current, interaction.guild.id)
    msg   = await interaction.channel.send(embed=embed, view=PlayerControls(player, interaction.guild.id))
    now_msgs[interaction.guild.id] = msg
    await interaction.response.send_message("✅ Updated!", ephemeral=True)

@bot.tree.command(name="history", description="Show recently played songs")
async def history(interaction: discord.Interaction):
    hist = get_history(interaction.guild.id)
    if not hist:
        return await interaction.response.send_message("📜 No history yet.")
    lines = [f"`{i+1}.` {t.title}" for i, t in enumerate(reversed(hist[-10:]))]
    embed = discord.Embed(title="📜 Recently Played", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="djrole", description="Set the DJ role (admin only)")
@app_commands.describe(role="The role to set as DJ")
async def djrole(interaction: discord.Interaction, role: discord.Role = None):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    if role is None:
        dj_roles.pop(interaction.guild.id, None)
        return await interaction.response.send_message("✅ DJ role removed.")
    dj_roles[interaction.guild.id] = role.id
    await interaction.response.send_message(f"✅ DJ role → **{role.name}**")

@bot.tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Join a voice channel first!", ephemeral=True)
    if interaction.guild.voice_client:
        return await interaction.response.send_message("✅ Already connected.", ephemeral=True)
    await interaction.user.voice.channel.connect(cls=wavelink.Player, self_deaf=True)
    await interaction.response.send_message("🎧 Joined!")

@bot.tree.command(name="help", description="Show all commands")
async def help(interaction: discord.Interaction):
    embed = discord.Embed(title="🎵 Music Bot Commands", color=0x5865F2)
    embed.add_field(name="▶️ Playback",
        value="`/play` `/pause` `/resume` `/stop` `/skip`", inline=False)
    embed.add_field(name="📋 Queue",
        value="`/queue` `/remove` `/clear` `/shuffle` `/history`", inline=False)
    embed.add_field(name="🔁 Modes",
        value="`/loop` `/autoplay` `/247`", inline=False)
    embed.add_field(name="🔊 Volume",
        value="`/volume`", inline=False)
    embed.add_field(name="🎛️ Filters",
        value="`/filter` — nightcore, speedup, slowed, vaporwave, bassboost, 8d, earrape, flat", inline=False)
    embed.add_field(name="⚙️ Settings",
        value="`/djrole` (admin only) `/nowplaying` `/join`", inline=False)
    embed.set_footer(text="🤖 Autoplay ON by default — bot never leaves mid-session!")
    await interaction.response.send_message(embed=embed)

# ── RUN ──────────────────────────────────────────────────────────────────────

bot.run(config.TOKEN)
