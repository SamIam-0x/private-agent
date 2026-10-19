# private-agent

A small agent that runs inside a [Tinfoil Container](https://docs.tinfoil.sh/containers/overview) and calls Tinfoil models from inside the enclave.

- `server.py` runs in the enclave. `POST /chat` takes the whole conversation, adds the system prompt, and calls the model (`kimi-k3` by default) with Tinfoil's private web search turned on. It returns either an answer or file-tool calls. It keeps no state and never logs message text.
- `app.py` + `ui.html` are a local chat page on the Mac (`http://127.0.0.1:8767`). The page uses `tinfoil.SecureClient` to check the enclave against this repo's published config before sending anything. It saves history encrypted (AES-256-GCM, key in the macOS Keychain) and runs the file tools on the Mac, limited to the folders chosen in Settings.

## Run the chat page

```bash
cd ~/Documents/GitHub/private-agent
.venv/bin/python app.py
```

## Deploy

The config repo must be public. The release workflow builds the image to `ghcr.io/samiam-0x/private-agent`.

1. Secrets: `TINFOIL_API_KEY` (an inference key, shared with other containers) and `AGENT_TOKEN` (made by `app.py setup-token` and kept in the Keychain).
   `.venv/bin/python app.py print-token | tinfoil secret create AGENT_TOKEN --value-file -`
2. `gh workflow run tinfoil-release.yml -f version=v0.0.1`, then wait for **Tinfoil Release - Publish**.
3. `tinfoil container create private-agent --repo SamIam-0x/private-agent --tag v0.0.1 --secret TINFOIL_API_KEY --secret AGENT_TOKEN`
4. Check: `tinfoil http get https://private-agent.personal-sam.containers.tinfoil.dev/health -e private-agent.personal-sam.containers.tinfoil.dev -r SamIam-0x/private-agent`

## Privacy limits

- Web search queries leave the enclave for Exa, which keeps no data under contract. PII masking is best-effort, so turn search off in Settings for questions that include names or account numbers.
- When the agent reads a file, its contents are sent into the enclave.
- The enclave's disk is wiped on restart, so history lives only on the Mac.
