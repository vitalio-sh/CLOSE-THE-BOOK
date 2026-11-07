"""Utilidades comunes: normalización de texto, selección robusta de columnas, tolerancia."""
import re
import unicodedata

import pandas as pd


def norm(s) -> str:
    """Mayúsculas, sin acentos, sólo letras/dígitos separados por un espacio."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", " ", s).strip()


def norm_doc(s) -> str:
    """Número de documento sin separadores: 'FV25-0400572' -> 'FV250400572'."""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def pick(df: pd.DataFrame, *cands, required=True):
    """Devuelve el primer nombre de columna que exista (sin distinguir mayúsculas).
    AJUSTAR: si FORMAT.md usa otros nombres, añádelos a la lista de candidatos."""
    cols = {c.lower(): c for c in df.columns}
    for c in cands:
        if c.lower() in cols:
            return cols[c.lower()]
    if required:
        raise KeyError(f"Ninguna de {cands} está en {list(df.columns)}")
    return None


def tol(x: float) -> float:
    """Tolerancia oficial de importe: max(0,01; 0,5 %)."""
    return max(0.01, 0.005 * abs(x))


def find(folder, name):
    """`name` o `name.gz` dentro de `folder` (los datos reales vienen en .csv.gz). None si no existe."""
    from pathlib import Path
    for n in (name, name + ".gz"):
        p = Path(folder) / n
        if p.exists():
            return p
    return None


def read_csv(path) -> pd.DataFrame:
    """CSV como texto. Los ficheros grandes (>20 MB) se cachean en models/cache como pickle."""
    import hashlib
    import os
    from pathlib import Path
    path = Path(path)
    if path.stat().st_size < 20e6:
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    st = path.stat()
    key = hashlib.md5(f"{path.resolve()}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:12]
    cp = Path("models/cache") / f"{path.name}.{key}.pkl"
    if cp.exists():
        return pd.read_pickle(cp)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    cp.parent.mkdir(parents=True, exist_ok=True)
    tmp = cp.with_suffix(f".tmp{os.getpid()}")
    df.to_pickle(tmp)
    tmp.rename(cp)
    return df


def to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", ".", regex=False), errors="coerce").fillna(0.0)
