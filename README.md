# echo-dot-grok

Turn a 2016 Echo Dot into a Wi-Fi handset for a [Grok Bot](https://x.ai) voice agent.

Unmute lifts the handset and opens a live voice call. Mute while the agent is talking cuts that reply and keeps the call open. Mute while it is only listening hangs up. Same idea as [this demo](https://x.com/MichaelGannotti/status/2113322549392159141): the Dot stays on Wi-Fi, and the button is the handset.

Tested on one Echo Dot 2nd gen, model RS03QR (codename biscuit), Fire OS 5.5.5.4, Android 5.1.1. Unlocked with [firebreak](https://github.com/bboe/firebreak), target `amonet-biscuit-v1.1.0-bboe`. Other Echo models do not have this unlock.

Not affiliated with Amazon or xAI. The HTTP calls are the Grok Bot app's private API. They break when the app changes.

You need the Dot and one Mac on the same LAN. The Mac stays logged into the Grok Bot app, awake, and runs both the mint and the bridge. Python 3. No extra Python packages. A Linux box can run the bridge instead. That split is in [Bridge on Linux](#bridge-on-linux). This document's path is the Mac. An agent adapting this to another LAN follows [AGENTS.md](AGENTS.md). The addresses in this repo are documentation (`192.0.2.0/24`). Replace them on the machine. Do not put real addresses back into the Python files.

## 1. Unlock the Dot

Once, over USB, from a machine with `adb`, `fastboot`, and `uv`:

```sh
uvx --system-certs firebreak@latest
```

Use the biscuit target. Leave the Alexa app alone before and after. A stock update can land a build firebreak cannot unlock. Rooting erases the Dot, including Wi-Fi.

Done when `adb shell su -c id` prints `uid=0`.

## 2. Put the Dot on Wi-Fi

Still over USB. The Dot has `wpa_cli` and a config at `/data/misc/wifi/wpa_supplicant.conf`. That file already has the socket path. Append one network block. Leave the lines that are already there.

On the computer, not on the Dot:

```sh
wpa_passphrase "Your SSID" 'your password' > /tmp/dot-net.txt
```

Open `/tmp/dot-net.txt` and delete the `#psk=` line. That line is the password in clear text. The `psk=` line under it is a hash. If `wpa_passphrase` is not installed and the password has no `"` or `\`, this block works instead:

```
network={
    ssid="Your SSID"
    psk="your password"
    key_mgmt=WPA-PSK
}
```

See which access point is 2.4 GHz. On the unit this was written for, 2.4 GHz joined and 5 GHz was flaky.

```sh
adb shell su -c 'ifconfig wlan0 up; wpa_cli -p /data/misc/wifi/sockets -i wlan0 scan'
sleep 3
adb shell su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 scan_results'
```

`scan` can print `FAIL-BUSY`. `scan_results` can still return rows. Then:

```sh
adb push /tmp/dot-net.txt /data/local/tmp/dot-net.txt
adb shell su -c 'cat /data/local/tmp/dot-net.txt >> /data/misc/wifi/wpa_supplicant.conf && rm /data/local/tmp/dot-net.txt && chown wifi:wifi /data/misc/wifi/wpa_supplicant.conf && chmod 660 /data/misc/wifi/wpa_supplicant.conf && svc wifi enable && wpa_cli -p /data/misc/wifi/sockets -i wlan0 reconfigure && wpa_cli -p /data/misc/wifi/sockets -i wlan0 save_config'
rm /tmp/dot-net.txt
adb shell su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 status'
```

Done when `status` shows `wpa_state=COMPLETED` and an `ip_address`. The Dot may not answer ping. The IP in that status is `DOT_IP`.

## 3. Wireless adb

USB still plugged in:

```sh
adb tcpip 5555
adb connect DOT_IP:5555
```

Unplug USB. From the Mac, `adb connect DOT_IP:5555` must print `connected`. One adb client holds this session. That client is the bridge. Port 5555 is a root shell. `adb` is [Android platform-tools](https://developer.android.com/tools/releases/platform-tools). On a Mac it is not part of the system.

## 4. Name the agent

On the Mac, in a GUI session, with Grok Bot installed and logged in. The keychain item is "Grok Bot Safe Storage". An SSH session cannot read it.

```sh
python3 mint_grok.py --list-agents
```

Each line is `agentId`, the name shown in the app, and `voiceId`. A voice of `-` means the app default. The numeric `id` is a different field and is not printed. Copy the uuid into `GROK_AGENT_ID` and the name into `GROK_AGENT_NAME`.

```sh
python3 mint_grok.py --self-check
```

prints `list-check ok` and does not talk to the app.

## 5. Mint on the Mac

The mint serves loopback `127.0.0.1:19773`. The bridge on this same Mac calls it directly. The bearer is not an xAI API key.

```sh
mkdir -p ~/.config/dot-voice
openssl rand -hex 32 > ~/.config/dot-voice/mint.token
chmod 600 ~/.config/dot-voice/mint.token
```

Copy this repo onto the Mac. Edit `examples/com.example.dot-grok-mint.plist`: absolute path to `mint_grok.py` (launchd does not expand `~`), log paths, `GROK_AGENT_ID`, `GROK_AGENT_NAME`. A GUI agent does not inherit your shell, so the two variables belong in the plist.

```sh
cp examples/com.example.dot-grok-mint.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.dot-grok-mint.plist
```

Already loaded: `launchctl bootout gui/$(id -u)/com.example.dot-grok-mint`, then bootstrap again.

Done when `~/Library/Logs/dot-grok-mint.log` contains `listening 19773`.

## 6. Bridge on the same Mac

`DOT_ADB_ALLOW` is this Mac's LAN address. Each entry is an IPv4 address. Anything else is refused before it can reach a shell on the Dot. Empty means 5555 stays open to the LAN. On a shared network, set the allow list.

Leave `DOT_MINT_SSH` unset. The bridge reads `~/.config/dot-voice/mint.token` and calls `127.0.0.1:19773`.

Edit `examples/com.example.dot-voice.plist`. Launchd does not expand `~`. `caffeinate -ims` keeps the Mac awake while the bridge runs. The Mac's own address goes in `DOT_ADB_ALLOW`.

```sh
cp examples/com.example.dot-voice.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.dot-voice.plist
```

Already loaded: `launchctl bootout gui/$(id -u)/com.example.dot-voice`, then bootstrap again.

macOS blocks a LaunchAgent from the LAN until the binary is allowed under Privacy & Security → Local Network. A terminal on that Mac can `adb connect` while the agent log says `adb not connected` or `No route to host`. Allow Python, `adb`, and `caffeinate` (if it is the parent) and bootstrap again.

Or run it in a terminal once:

```sh
export ADB=/usr/local/bin/adb
export DOT_IP=192.0.2.50
export DOT_SERIAL=192.0.2.50:5555
export DOT_ADB_ALLOW=192.0.2.20
python3 dot_voice.py
```

`python3 dot_voice.py --self-check` needs no Dot and no environment. It prints `self-check ok 22c3ec84`.

Done when `~/Library/Logs/dot-voice.log` says `handset up. unmute opens the line. mute closes it.` Startup plays the lift click once, with no call open. That click is the speaker check.

The bridge then installs iptables on the Dot: ACCEPT for `DOT_ADB_ALLOW`, DROP for everyone else. Those rules die when the Dot reboots and return the next time the bridge starts.

## Bridge on Linux

The mint cannot move. It reads the Mac keychain, and an SSH session cannot. A Linux box can own `adb` and the audio instead of the Mac. One adb client still. Stop the Mac bridge before the Linux one connects.

On the Mac, the mint from section 5 stays up. Add one key from the Linux box, no passphrase:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/dot-mint -N ""
```

On the Mac, one line in `~/.ssh/authorized_keys`. `restrict` turns off pty, forwarding, and a shell. `mint-client.sh` must be executable. `base64 -D` is the macOS flag.

```
command="/Users/YOU/echo-dot-grok/mint-client.sh",restrict ssh-ed25519 AAAA... dot-bridge
```

`AAAA...` is the contents of `~/.ssh/dot-mint.pub`.

From the Linux box:

```sh
ssh -i ~/.ssh/dot-mint -o IdentitiesOnly=yes you@192.0.2.20 mint
```

Done when that exits 0 and the body is one JSON object whose `websocketUrl` starts with `wss://`. That body is a call secret. Do not commit it, and do not paste it into a chat.

```sh
mkdir -p ~/.config/systemd/user
cp examples/dot-voice.service ~/.config/systemd/user/dot-voice.service
```

Edit the copied unit. Set `DOT_MINT_SSH`, `DOT_MINT_KEY`, `DOT_IP`, and `DOT_ADB_ALLOW` to the Linux box's own address. Then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now dot-voice.service
loginctl enable-linger "$USER"
```

Linger keeps the user service up after logout. With `DOT_MINT_SSH` set, the bridge uses SSH and ignores the local token.

## 7. Talk

Unmute. You hear the lift, the ring breathes cyan (`active-talking`), and the agent is on the line. Speak. Mute while it is only listening: heavier click, hangup, ring off. Mute while it is talking: the reply stops, the call stays open, no close click.

A loud voice at the Dot can also cut a reply. A quiet voice from across the room cannot. There is no echo canceller.

## If it stays silent

- The button works and the speaker does not: `adb shell su -c 'setprop ctl.restart media'`, then restart the bridge so it sets the HD switch and the volume again.
- Purple ring: the Dot was never finished in the Alexa app. Idle clears `anim_OOBE_start_error`. Restart the bridge and check `adb shell su -c '/system/bin/ledctrl --get_active_patterns'`. Idle is `off`.
- `adb not connected`: another computer holds the wireless session. Close that session. The bridge connects.
- Mint log says `mint fail`: Grok Bot is logged out, the GUI session cannot read the keychain, or the private API moved. The paths are in `mint_grok.py`.
- Two spoken replies for one tool, or a tool result with no sound: say what the log printed (`reply`, `speak`, `said`) before changing `reply_gate`.

## What the pieces are

| File | Runs on |
| --- | --- |
| `dot_voice.py` | the Mac, next to the mint. A Linux box only if you split it |
| `Dotd.java`, `Playd.java`, `dotd.dex` | the Dot, pushed at each start |
| `mint_grok.py` | the Mac |
| `mint-client.sh` | the Mac, only when the bridge is on another machine |
| `examples/` | placeholders |

`Dotd` captures the mic at 16 kHz and reads the mute key. `Playd` plays 48 kHz in its own process. Fire OS kills an `app_process` that starts a second Java thread. Killing every `app_process` kills zygote.

Measured on this biscuit, card `mtsndcard`:

- `AudioTrack` buffer stays 32768 bytes. Under that, this Fire OS opens a DIRECT track and the cone underruns into static.
- `Audio_I2S0dl1_hd_Switch` is `On` before the track opens. `Off` plays chopped blasts.
- `PCM Playback Volume` is `115 115`. `100` is too quiet. `140` crackles. Volume applies when the track opens.
- Mic 16 kHz, API session 24 kHz, speaker 48 kHz. The bridge resamples.
- `ledctrl --set_pattern=dialog-talk` prints success and does not stick. That name is absent from `/system/etc/led-resources/layer_config_common.json`. `active-talking` stays until unset.

Rebuild `dotd.dex` only when the Java changes. `javac` 17, `--release 8`, then `d8 --min-api 21`. `d8 --output` is a directory. It writes `classes.dex`. Rename that to `dotd.dex`.

```sh
javac --release 8 -cp "$ANDROID_JAR" Dotd.java Playd.java
mkdir -p out
d8 --min-api 21 --output out Dotd.class Playd.class
mv out/classes.dex dotd.dex
```

`ANDROID_JAR` is `android.jar` from an Android SDK platform (35 is enough).

## Security

- `adb` on tcp/5555 is root. Allowlist it.
- `~/.config/dot-voice/mint.token` is a bearer for loopback on the Mac.
- With the bridge on the Mac, nothing remote can mint a call. The token stays on loopback.
- With the bridge on Linux, that SSH key can mint a call and run one tool. With the forced command, it gets no shell.
- The call secret stays in the bridge process for that call.
- Keep `mint.token`, SSH private keys, `wpa` configs, and `~/Library/Application Support/Grok Bot/sand-secrets.json` out of git.
- Unofficial endpoints, host `api2.cursor.sh`: `ListGrokBotAgents`, `DashboardService/GetMe`, `MintSandVoiceCallSecret`, `VoiceCallHarnessSession`, `VoiceCallHarnessTool`. The voice websocket is on `api.x.ai`. The mint returns the model. When this was written, that model was `grok-voice-synapse`.

## Not in this repo

- A wake word.
- The agent calling you.
- Resuming the open Grok Bot chat across unmute. Each unmute is a fresh call. The agent's own tools can look old chats up.
- Echo cancellation.

## License

MIT. Copyright Jean-Baptiste Ronssin, 2026.
