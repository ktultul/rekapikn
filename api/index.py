# Entry point khusus Vercel. Vercel mensyaratkan file Python-nya ada di
# dalam folder api/, jadi file ini cuma "meneruskan" ke app Flask yang
# sebenarnya di app.py (root project) — supaya app.py tetap satu-satunya
# sumber kebenaran, tidak perlu duplikat kode.
from app import app  # noqa: F401
