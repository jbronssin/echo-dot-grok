# Local setup

Use this when adapting the handset to another Dot, another LAN, another Mac, or another Grok Bot agent. The human order is README sections 1 through 7. Follow that order. A local setup is environment plus two installed files. Python defaults stay empty.

## Collect

Ask for each value. Do not invent one, and do not copy one out of a different checkout.

- `DOT_IP` from `wpa_cli status` after README section 2 (`ip_address`).
- Bridge LAN address, for `DOT_ADB_ALLOW`.
- `ADB` path on the bridge. `adb` on `PATH` is valid.
- Mac `user@host` reachable from the bridge on the LAN, and the SSH private key path on the bridge.
- Absolute path of this repo on the Mac and on the bridge. Launchd does not expand `~`.
- `GROK_AGENT_ID` and `GROK_AGENT_NAME` from `python3 mint_grok.py --list-agents`, run in the Mac GUI session. The line is `uuid`, name, `voiceId`.

## Write

Copy, then edit the copies:

- Bridge: `examples/dot-voice.service` to `~/.config/systemd/user/dot-voice.service`. Then `systemctl --user daemon-reload` and `enable --now`. `loginctl enable-linger` for the bridge user.
- Mac: `examples/com.example.dot-grok-mint.plist` to `~/Library/LaunchAgents/`. Set `ProgramArguments`, both log paths, `GROK_AGENT_ID`, `GROK_AGENT_NAME`. `launchctl bootstrap gui/$(id -u)`.

Tracked `examples/` keep `192.0.2.0/24`, `/home/you`, `/Users/YOU`, the zero uuid, and `Your agent`.

Mint bearer, on the Mac:

```sh
openssl rand -hex 32 > ~/.config/dot-voice/mint.token
chmod 600 ~/.config/dot-voice/mint.token
```

Bridge key: `ssh-keygen -t ed25519 -f ~/.ssh/dot-mint -N ""`. Mac `authorized_keys` line is in README section 5. `mint-client.sh` is executable.

## Done when

All of these are true:

1. `python3 dot_voice.py --self-check` prints `self-check ok 22c3ec84`.
2. `python3 mint_grok.py --self-check` prints `list-check ok`.
3. The mint log contains `listening 19773`.
4. From the bridge, `ssh -i "$DOT_MINT_KEY" -o IdentitiesOnly=yes "$DOT_MINT_SSH" mint` exits 0 and the body is JSON. Leave that body on the terminal. It is a call secret.
5. The bridge log contains `handset up. unmute opens the line. mute closes it.` The lift click played, and no call was opened by the restart.
6. Unmute opens a call. Mute while the line is only listening hangs up.

## Leave these alone

Measured on RS03QR / biscuit / Fire OS 5.5.5.4. A new LAN does not retune them.

- `Playd` `CAP` 32768. A smaller buffer opens a DIRECT track and the cone statics.
- `Audio_I2S0dl1_hd_Switch` `On` before the track opens. Volume `115 115` before the speaker socket connects. Mid-playback volume does not silence the cone.
- `Dotd` and `Playd` are two processes. One Java thread each. A second thread gets the process killed.
- Rates 16000 in, 24000 on the API session, 48000 out.
- `MIC_GAIN` 6, `BARGE_RMS` 1600, playback hold 0.8 s. There is no echo canceller.
- Ring pattern `active-talking`. Idle also unsets `anim_OOBE_start_error`. `dialog-talk` does not stick.
- `reply_gate` sends one `speak` nudge when the turn after a tool closes with no audio. A second silent close does not send. A user turn with no tool does not send.

A Java edit is a dex rebuild in the same commit (README, "What the pieces are"). An agent change or a voice change is the plist. No dex rebuild.

## When the private API moves

The `api2.cursor.sh` paths live in `mint_grok.py`. A mint log of `mint fail` plus `upstream` means the app moved them. Update those URL constants. The call still comes from the Grok Bot app's mint.

## Before a commit

`git status` and `git grep` the diff. Real LAN addresses, agent uuids, agent names, `mint.token`, private keys, `wpa` configs, and call JSON belong in the installed unit, the installed plist, and the home directory. The tracked examples stay the placeholders above.
