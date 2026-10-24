# echo-dot-grok

Turn a 2016 Echo Dot into a Wi-Fi handset for a [Grok Bot](https://x.ai) voice agent.

Unmute lifts the handset and opens a live voice call. Mute while the agent is talking cuts that reply and keeps the call open. Mute while it is only listening hangs up. Same idea as [this demo](https://x.com/MichaelGannotti/status/2113322549392159141): the Dot stays on the house Wi-Fi, and the button is the handset.

Tested on one Echo Dot 2nd gen, model RS03QR (codename biscuit), Fire OS 5.5.5.4, Android 5.1.1. Unlocked with [firebreak](https://github.com/bboe/firebreak), target `amonet-biscuit-v1.1.0-bboe`. Other Echo models do not have this unlock.

Not affiliated with Amazon or xAI. The HTTP calls are the Grok Bot app's private API. They break when the app changes.

## What you need

Three machines, all yours:

1. The Dot, on the same LAN as the bridge. Do not finish setup in the Alexa app. A stock update can land a build firebreak cannot unlock.
2. A Linux box on that LAN, left on. It owns wireless adb and the audio bridge. Python 3 from the OS. No extra packages.
3. A Mac that stays logged into the Grok Bot app. The mint reads the keychain item "Grok Bot Safe Storage". A GUI session can read it. An SSH session cannot. The Mac does not carry the audio.

Wake word and incoming calls are not in this repo. Each unmute starts a fresh call. Old chats are not copied in. The agent's own tools can look them up.

## Root

Once, over USB, from a machine with `adb`, `fastboot`, and `uv`:

```sh
uvx --system-certs firebreak@latest
```

Use the biscuit target. When it boots, the Dot is rooted Fire OS with TWRP and Magisk.

Join Wi-Fi on the device. This repo does not store a network. Then:

```sh
adb tcpip 5555
adb connect DOT_IP:5555
```

Port 5555 is a root shell. Set `DOT_ADB_ALLOW` to the bridge, and to any other host that should keep adb. The bridge installs an ACCEPT for each of those addresses and a DROP for everyone else. The rules die on reboot and come back the next time the bridge starts. Leave `DOT_ADB_ALLOW` empty and 5555 stays open on the LAN. Do not do that on a shared network.

One adb client holds the wireless session. That client should be the bridge.

## Layout

| File | Runs on |
| --- | --- |
| `dot_voice.py` | the Linux bridge |
| `Dotd.java`, `Playd.java`, `dotd.dex` | the Dot, pushed at each start |
| `mint_grok.py` | the Mac, loopback `127.0.0.1:19773` |
| `mint-client.sh` | the Mac, as a forced SSH command |
| `examples/` | a user unit and a launchd agent, with placeholder addresses |

`Dotd` captures the mic at 16 kHz and reads the mute key. `Playd` plays 48 kHz in its own process. Fire OS kills an `app_process` that starts a second Java thread. Do not kill zygote. Killing every `app_process` is killing zygote.

## Build the dex

`dotd.dex` matches the Java in this tree. Rebuild it when you change the Java. `javac` 17, `--release 8`, then `d8 --min-api 21`. `d8 --output` has to be a directory. It writes `classes.dex`. Rename that to `dotd.dex`.

```sh
javac --release 8 -cp "$ANDROID_JAR" Dotd.java Playd.java
mkdir -p out
d8 --min-api 21 --output out Dotd.class Playd.class
mv out/classes.dex dotd.dex
```

`ANDROID_JAR` is `android.jar` from an Android SDK platform (35 is enough). `d8` is in the SDK build-tools.

## Mint

On the Mac, with Grok Bot installed and logged in.

The agent id is a uuid on the agent row, field `agentId`. The numeric `id` is a different field. Voice is `voiceId` on that same row. Null means the app default: the mint omits `voice`.

The list call is unofficial. It moves with the app:

```
POST https://api2.cursor.sh/aiserver.v1.GrokBotService/ListGrokBotAgents
Authorization: Bearer <the app token>
Content-Type: application/json
Connect-Protocol-Version: 1

{}
```

`mint_grok.py` already does this with the token it decrypts from the app. You only need the uuid and the display name.

Create the bearer the bridge will send. It is not an xAI API key.

```sh
mkdir -p ~/.config/dot-voice
openssl rand -hex 32 > ~/.config/dot-voice/mint.token
chmod 600 ~/.config/dot-voice/mint.token
```

Copy this repo onto the Mac. Edit `examples/com.example.dot-grok-mint.plist`. Launchd does not expand `~`, so the path to `mint_grok.py` is absolute. Set `GROK_AGENT_ID` and `GROK_AGENT_NAME`. The name is the one shown in the app. The harness uses it. The bridge does not type it into a prompt.

A GUI agent does not inherit your shell, so those two variables belong in the plist.

```sh
cp examples/com.example.dot-grok-mint.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.dot-grok-mint.plist
```

It listens on `127.0.0.1:19773` only. A GET without the bearer returns 401. Logs say `mint ok`, the tool count, and the voice id. They do not print the call secret, the persona, or the receipt text.

SSH from the Linux box must not get a shell. On the Mac, one key, forced command, in `~/.ssh/authorized_keys`:

```
command="/Users/YOU/echo-dot-grok/mint-client.sh",restrict ssh-ed25519 AAAA... dot-bridge
```

`restrict` turns off pty, forwarding, agent forwarding, and X11. `mint-client.sh` accepts `mint`, or `tool` plus one base64 JSON blob. `base64 -D` is the macOS flag, so the mint host is a Mac. Generate the keypair on the bridge and append only the public line. `chmod +x mint-client.sh`.

## Bridge

Replace the documentation addresses (`192.0.2.0/24` is not a real LAN) and the paths:

```sh
export ADB=/usr/bin/adb
export DOT_IP=192.0.2.50
export DOT_SERIAL=192.0.2.50:5555
export DOT_ADB_ALLOW=192.0.2.10
export DOT_MINT_SSH=you@192.0.2.20
export DOT_MINT_KEY=/home/you/.ssh/dot-mint
python3 dot_voice.py
```

`DOT_ADB_ALLOW` is a comma-separated list of IPv4 addresses. Anything else is refused before it can reach a shell on the Dot.

Or install `examples/dot-voice.service` as a user unit and edit the same values:

```sh
systemctl --user enable --now dot-voice.service
```

Startup plays the lift click once, with no call open. That is the speaker check.

```sh
python3 dot_voice.py --self-check
```

needs no Dot and no environment. It prints `self-check ok 22c3ec84`.

## A call

Unmute plays a short lift and opens a Grok Bot voice session. The ring runs the registered pattern `active-talking` (a cyan breath) for the length of the call.

Mute during playback cuts the reply, stays on the call, and plays no close click. A loud voice at the Dot can also cut: raw RMS 1600 for two 100 ms chunks. A quiet voice from across the room does not. There is no echo canceller. The mic is dropped while speaker audio is in flight, plus 0.8 s, so the bot does not hear the speaker.

Mute while the line is only listening plays a heavier set-down and hangs up. The ring goes idle. Idle also clears `anim_OOBE_start_error`. That pattern is the purple setup pulse on a Dot that was never finished in the Alexa app. Brightness is left alone.

If the turn after a tool closes with no audio, the bridge asks once more, with a one-turn instruction to speak the tool result. A second silent close does not ask again. The log line is `speak`. A user turn with no tool and no audio is left alone. A follow-up that already played audio is not repeated.

## Speaker settings that mattered

Measured on this biscuit, card `mtsndcard`:

- The `AudioTrack` buffer stays 32768 bytes. Under that, this Fire OS opens a DIRECT track and the cone underruns into static.
- `Audio_I2S0dl1_hd_Switch` is `On` before the track opens. `Off` plays a tone as chopped blasts.
- `PCM Playback Volume` is `115 115`. `100` is too quiet. `140` crackles. The volume is applied when the track opens. Changing it mid-playback does not silence the cone.
- Mic is 16 kHz mono s16. The API session is 24 kHz. The speaker is 48 kHz. The bridge resamples.
- `ledctrl --set_pattern=dialog-talk` prints success and does not stick. That name is not in `/system/etc/led-resources/layer_config_common.json`. `active-talking` is, and it stays until unset.
- A wedged `mediaserver` leaves the button working and the speaker silent. `adb shell su -c 'setprop ctl.restart media'`, set the HD switch and the volume again, then restart the bridge.

## Security

- `adb` on tcp/5555 is root. Allowlist it.
- `~/.config/dot-voice/mint.token` is a bearer for loopback on the Mac. The SSH key can mint a call and run one tool. With the forced command, it cannot open a shell.
- The call secret is short-lived. It stays in the bridge process for that call. Do not log it.
- Do not commit `mint.token`, SSH private keys, or `~/Library/Application Support/Grok Bot/sand-secrets.json`.
- These endpoints are private and unofficial. Host `api2.cursor.sh`:
  - `GrokBotService/ListGrokBotAgents`
  - `DashboardService/GetMe`
  - `GrokBotService/MintSandVoiceCallSecret`
  - `GrokBotService/VoiceCallHarnessSession`
  - `GrokBotService/VoiceCallHarnessTool`
- The voice websocket is on `api.x.ai`. The mint returns the model. When this was written, that model was `grok-voice-synapse`.

## Not in this repo

- A wake word.
- The agent calling you.
- Resuming the open Grok Bot chat across unmute.
- Echo cancellation.

## License

MIT. Copyright Jean-Baptiste Ronssin, 2026.
