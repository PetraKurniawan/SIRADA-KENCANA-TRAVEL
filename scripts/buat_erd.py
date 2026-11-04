"""Membuat ERD (Mermaid) langsung dari model SQLAlchemy, sehingga SELALU sinkron dengan database.

Jalankan dari folder backend:   python scripts/buat_erd.py            -> docs/ERD.md
Cek sinkron (untuk CI/uji):     python scripts/buat_erd.py --cek      -> exit 1 bila docs/ERD.md usang

Sumber kebenaran ERD adalah app/models/models.py (yang dipakai migrasi & bootstrap), bukan
gambar manual. Bila model berubah, jalankan ulang skrip ini dan tempel diagram ke laporan.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import UniqueConstraint  # noqa: E402

from app.core.database import Base  # noqa: E402
from app.models import models  # noqa: E402,F401  (mendaftarkan semua tabel ke Base.metadata)

OUT = ROOT / "docs" / "ERD.md"


def _tipe(kolom) -> str:
    return (str(kolom.type).split("(")[0].replace(" ", "_") or "TEXT").upper()


def buat_mermaid() -> str:
    tabel = Base.metadata.sorted_tables
    baris = ["erDiagram"]
    relasi = set()
    for t in tabel:
        fk_kolom = {c.name for c in t.columns if c.foreign_keys}
        for fk in t.foreign_key_constraints:
            tujuan = fk.referred_table.name
            wajib = all(not t.c[c.name].nullable for c in fk.columns)
            kiri = "||" if wajib else "|o"
            relasi.add(f'    {tujuan} {kiri}--o{{ {t.name} : "{", ".join(c.name for c in fk.columns)}"')
        baris.append(f"    {t.name} {{")
        for c in t.columns:
            tag = []
            if c.primary_key:
                tag.append("PK")
            if c.name in fk_kolom:
                tag.append("FK")
            if c.unique and not c.primary_key:
                tag.append("UK")
            baris.append(f"        {_tipe(c)} {c.name}" + (f" {','.join(tag)}" if tag else ""))
        baris.append("    }")
    baris += sorted(relasi)
    return "\n".join(baris)


def buat_dokumen() -> str:
    tabel = Base.metadata.sorted_tables
    md = ["# ERD Database SIRADA Kencana", "",
          "> Dibuat otomatis oleh `scripts/buat_erd.py` dari `app/models/models.py`. "
          "**Jangan diedit manual** - jalankan ulang skrip bila model berubah.", "",
          "## Diagram", "", "```mermaid", buat_mermaid(), "```", "",
          "## Kamus Data", ""]
    for t in tabel:
        md += [f"### `{t.name}`", "", "| Kolom | Tipe | Kunci | Null | Keterangan |", "|---|---|---|---|---|"]
        for c in t.columns:
            kunci = []
            if c.primary_key:
                kunci.append("PK")
            for fk in c.foreign_keys:
                kunci.append(f"FK -> {fk.column.table.name}.{fk.column.name}")
            if c.unique and not c.primary_key:
                kunci.append("UNIQUE")
            md.append(f"| {c.name} | {_tipe(c)} | {', '.join(kunci)} | {'Ya' if c.nullable else 'Tidak'} | |")
        gabungan = [fk for fk in t.foreign_key_constraints if len(fk.columns) > 1]
        for fk in gabungan:
            md.append(f"\nFK gabungan `{fk.name}`: ({', '.join(c.name for c in fk.columns)}) -> "
                      f"{fk.referred_table.name}({', '.join(e.column.name for e in fk.elements)})")
        uq = [c for c in t.constraints if isinstance(c, UniqueConstraint) and len(c.columns) > 1]
        for c in uq:
            md.append(f"\nUNIQUE gabungan `{c.name}`: ({', '.join(col.name for col in c.columns)})")
        md.append("")
    return "\n".join(md)


if __name__ == "__main__":
    isi = buat_dokumen()
    if "--cek" in sys.argv:
        ada = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if ada != isi:
            print("docs/ERD.md TIDAK sinkron dengan model. Jalankan: python scripts/buat_erd.py")
            sys.exit(1)
        print("ERD sinkron dengan model.")
    else:
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(isi, encoding="utf-8")
        print(f"Tulis {OUT}")
