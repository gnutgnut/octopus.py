# Personal Config Backup

`.env.gpg` is a GPG-encrypted copy of the project `.env` file containing:

- Octopus Energy API key and account details
- MPAN, serial number, and tariff code
- Telegram bot token and chat ID

## Decrypt

```bash
gpg -d personal/.env.gpg > .env
```

Enter the passphrase when prompted.

## Re-encrypt after changes

```bash
gpg -c .env
mv .env.gpg personal/
```

## Details

- Cipher: AES-256-CFB
- Key derivation: iterated+salted S2K (65 million iterations)
