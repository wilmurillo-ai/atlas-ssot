#!/usr/bin/env python3
import json
import urllib.request
import threading
import time
import queue
import re
import os

# Config properties
BASE_URL = "http://127.0.0.1:3845"
sse_url = f"{BASE_URL}/sse"
post_url = None
event_queue = queue.Queue()

# Target files
FILE_KEY = "bVRNvc1roScaoHUlBABt9T"
PAGE_ID = "0:1" # "Design Guidelines" page
DESIGN_MD_PATH = "/Users/wmurillo/Desktop/hermes/Atlas/DESIGN.md"

def sse_listener():
    global post_url
    req = urllib.request.Request(sse_url, headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(req) as response:
            current_event = None
            for line_bytes in response:
                line = line_bytes.decode("utf-8").strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
                    event_queue.put((current_event, data_str))
                    if current_event == "endpoint":
                        if data_str.startswith("http"):
                            post_url = data_str
                        else:
                            post_url = f"{BASE_URL}{data_str}"
    except Exception:
        pass

# Start the thread to discover post endpoint
t = threading.Thread(target=sse_listener, daemon=True)
t.start()

# Wait for background discovery
for _ in range(30):
    if post_url is not None:
        break
    time.sleep(0.1)

if post_url is None:
    print("❌ Error: Could not establish SSE connection to local Figma Dev Mode server.")
    print("   Make sure the Figma Desktop app is open and Dev Mode is active.")
    exit(1)

def send_post(payload):
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        post_url, data=data_bytes,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status

def wait_for_message(request_id, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            event_type, data_str = event_queue.get(timeout=0.1)
            if event_type == "message":
                msg = json.loads(data_str)
                if msg.get("id") == request_id:
                    return msg
        except queue.Empty:
            continue
    return None

try:
    # 1. Handshake
    init_id = 991
    send_post({
        "jsonrpc": "2.0", "id": init_id, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "hermes-token-syncer", "version": "1.0"}
        }
    })
    wait_for_message(init_id)
    send_post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    time.sleep(0.1)

    # 2. Get Metadata for Guidelines Page
    print(f"🔄 Pulling variable changes from Figma sheet page '{PAGE_ID}'...")
    call_id = 992
    send_post({
        "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
        "params": {
            "name": "get_metadata",
            "arguments": {"fileKey": FILE_KEY, "nodeId": PAGE_ID}
        }
    })
    resp = wait_for_message(call_id, timeout=20)
    if not resp or resp.get("isError"):
        print("❌ Error: Failed to retrieve file metadata from Figma MCP.")
        exit(1)

    full_text = ""
    for c in resp.get("result", {}).get("content", []):
        full_text += c.get("text", "")

    # Extract clean color specifications
    # We look for `<text id="..." name="COLOR_NAME" ... /> <text id="..." name="#HEX_VALUE" ... />` elements
    # Since they are located in pairs inside color swatches, we can grep them sequentially or map them.
    # Group names are organized into Frames, e.g. `<frame id="1:1647" name="Frame 14">` that contains text labeled `Primary` and `#HEX`
    color_map = {}
    
    # Simple regex parsing matching how the Figma Dev Mode MCP represents swatches:
    # <frame name="Frame XX"> with text structures
    frames_matches = re.findall(r'<frame[^>]*name="Frame \d+"[^>]*>([\s\S]*?)</frame>', full_text)
    for fm in frames_matches:
        texts = re.findall(r'name="([^"]+)"', fm)
        if len(texts) >= 2:
            name, hex_val = texts[0], texts[1]
            if hex_val.startswith("#") and len(hex_val) == 7:
                # Clean identifier, e.g. "Primary-Dark" to lower-case slashes / kebab
                clean_name = name.strip().lower().replace(" ", "-")
                color_map[clean_name] = hex_val.upper()

    # Manual backup fallbacks from visual guidelines to guarantee no loss
    # These are the ACTUAL Figma variable fallback values (verified via MCP get_design_context)
    fallbacks = {
        "primary": "#237FE1",
        "primary-dark": "#0563C7",
        "primary-light": "#ABD2FB",
        "secondary": "#EB9F0A",
        "secondary-dark": "#BD7D00",
        "secondary-light": "#FFF0D2",
        "complementary": "#3C8500",
        "complementary-dark": "#2C6100",
        "complementary-light": "#CFE1A8",
        "destructive": "#E1430A",
        "destructive-dark": "#A1330B",
        "destructive-light": "#FFC0A9",
        "black": "#31373D",
        "gray-100": "#707172",
        "gray-75": "#A4A4A4",
        "gray-50": "#D9DADB",
        "gray-25": "#EBEBEB",
        "white": "#F5F5F5"
    }

    # Overlay parsed tokens onto fallbacks
    final_colors = fallbacks.copy()
    for k, v in color_map.items():
        if k in final_colors:
            final_colors[k] = v

    print(f"🎨 Synced Color Palette: {len(color_map)} parsed from Figma:")
    for k, v in sorted(final_colors.items()):
        print(f"   • {k}: {v}")

    # Read current DESIGN.md
    if os.path.exists(DESIGN_MD_PATH):
        with open(DESIGN_MD_PATH, "r") as f:
            content = f.read()
    else:
        content = ""

    # Replace colors section in YAML frontmatter dynamically
    if content:
        # Find colors: block inside the frontmatter and update values
        colors_block = "colors:\n"
        # Always mapping keys
        colors_block += f"  primary: \"{final_colors['primary']}\"\n"
        colors_block += f"  primary-dark: \"{final_colors['primary-dark']}\"\n"
        colors_block += f"  primary-light: \"{final_colors['primary-light']}\"\n"
        colors_block += f"  secondary: \"{final_colors['secondary']}\"\n"
        colors_block += f"  secondary-dark: \"{final_colors['secondary-dark']}\"\n"
        colors_block += f"  secondary-light: \"{final_colors['secondary-light']}\"\n"
        colors_block += f"  complementary: \"{final_colors['complementary']}\"\n"
        colors_block += f"  complementary-dark: \"{final_colors['complementary-dark']}\"\n"
        colors_block += f"  complementary-light: \"{final_colors['complementary-light']}\"\n"
        colors_block += f"  destructive: \"{final_colors['destructive']}\"\n"
        colors_block += f"  destructive-dark: \"{final_colors['destructive-dark']}\"\n"
        colors_block += f"  destructive-light: \"{final_colors['destructive-light']}\"\n"
        colors_block += f"  neutral-text: \"{final_colors['black']}\"\n"
        colors_block += f"  neutral-muted: \"{final_colors['gray-100']}\"\n"
        colors_block += f"  neutral-border: \"{final_colors['gray-25']}\"\n"
        colors_block += f"  neutral-bg: \"{final_colors['white']}\"\n"

        # Regex replace the colors section between `colors:` and `typography:`
        new_content = re.sub(r'colors:\n(.*?)(?=typography:)', colors_block, content, flags=re.DOTALL)
        
        # Write back
        with open(DESIGN_MD_PATH, "w") as f:
            f.write(new_content)
        print(f"✅ Success! Updated '{DESIGN_MD_PATH}' with fresh design tokens.")
    else:
        print("❌ Error: Target DESIGN.md file not found to inject tokens.")

except Exception as e:
    print(f"❌ Synchronizer failed: {e}")
