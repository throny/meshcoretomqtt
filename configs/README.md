# Configuration Templates

This directory is for community-contributed configuration templates.

## Usage

Use the `--config` flag with the installer to apply a hosted configuration:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/install.sh | \
  bash -s -- --config https://example.com/path/to/config.env
```

## Example: Dual Broker Setup

A common configuration is to report to both a local MQTT broker and the LetsMesh public observer network:

**Local MQTT + LetsMesh.net Packet Analyzer** - `example-dual-broker.env`:
```bash
# Location (3-letter airport code)
MCTOMQTT_IATA=XXX

# Serial Configuration
MCTOMQTT_SERIAL_PORTS=/dev/ttyACM0

# Broker 1 - Local MQTT Server
MCTOMQTT_MQTT1_ENABLED=true
MCTOMQTT_MQTT1_SERVER=mqtt.local
MCTOMQTT_MQTT1_PORT=1883
MCTOMQTT_MQTT1_USERNAME=myuser
MCTOMQTT_MQTT1_PASSWORD=mypass

# Broker 2 - LetsMesh.net Packet Analyzer
MCTOMQTT_MQTT2_ENABLED=true
MCTOMQTT_MQTT2_SERVER=mqtt-us-v1.letsmesh.net
MCTOMQTT_MQTT2_PORT=443
MCTOMQTT_MQTT2_TRANSPORT=websockets
MCTOMQTT_MQTT2_USE_TLS=true
MCTOMQTT_MQTT2_USE_AUTH_TOKEN=true
MCTOMQTT_MQTT2_TOKEN_AUDIENCE=mqtt-us-v1.letsmesh.net
```

## Creating Your Own Configuration

You can host your own configuration file anywhere (GitHub Gist, your repo, etc.):

1. Copy `.env.local.example` as a template
2. Configure for your region/network
3. Host it publicly (Gist, repo, etc.)
4. Use the URL with `--config`

### Example: GitHub Gist

```bash
# Create a Gist with your config
# Get the raw URL, then:

curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/install.sh | \
  bash -s -- --config https://gist.githubusercontent.com/username/abc123/raw/my-config.env
```

### Example: Your Own Repository

If you're maintaining a fork or custom branch:

```bash
curl -fsSL https://raw.githubusercontent.com/yourusername/meshcoretomqtt/yourbranch/install.sh | \
  bash -s -- \
    --config https://raw.githubusercontent.com/yourusername/meshcoretomqtt/yourbranch/configs/.env.yourregion \
    --repo yourusername/meshcoretomqtt \
    --branch yourbranch
```

## Configuration Format

All configuration variables should use the `MCTOMQTT_` prefix and follow `.env` format.

See the main [README.md](../README.md) for all available configuration options.

## Hosting Your Configuration

You can host configuration files anywhere publicly accessible:

### Option 1: GitHub Gist (Recommended)
1. Create a new Gist at https://gist.github.com
2. Name it `mctomqtt-config.env`
3. Paste your configuration
4. Get the "Raw" URL
5. Use with `--config`

### Option 2: Your Own Repository
Store configs in your own repo and reference them:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/install.sh | \
  bash -s -- --config https://raw.githubusercontent.com/yourusername/yourrepo/main/mctomqtt.env
```

### Option 3: Web Server
Host the config file on any web server and provide the URL.

## Contributing

Community configuration examples are welcome! To contribute:

1. Fork the repository
2. Add your example to this README (not as a file)
3. Include comments explaining the use case
4. Submit a pull request

**Note:** Don't include passwords or private keys in examples!

