"""Bounded advance metrics for the two local Bigfont profiles in our samples.

Glyph outlines still use Unicode substitution. GBCBIG's internal scale wrappers
produce coordinates on an 8-unit em; HZTXT uses its declared 127-unit em.
GBCBIG's 5.8235294118-unit advance / 8 is independently checked against the
adjacent authored TEXT insertion points in samples 61/62 (handles FE6/FE7).
Other Bigfont formats, encodings and extended subshape calls are not inferred.
"""
from functools import lru_cache
from pathlib import Path
import struct
from ezdxf.fonts import shapefile


class BigfontMetrics:
    def __init__(self, path: Path):
        self.name=path.name
        if path.stem.lower() not in ('hztxt','gbcbig'):
            raise ValueError('Unsupported Bigfont metric profile')
        data=path.read_bytes()
        if len(data)>16_000_000 or not data.startswith(b'AutoCAD-86 bigfont 1.0'):
            raise ValueError('Unsupported Bigfont file')
        start=data.index(b'\x1a')+1
        size,count,ranges=struct.unpack_from('<HHH',data,start)
        if size!=8 or ranges>256 or count>65535:
            raise ValueError('Invalid Bigfont index')
        index=start+6+ranges*4
        self.font=shapefile.ShapeFile(path.stem.encode(),127 if path.stem.lower()=='hztxt' else 8,0)
        for i in range(count):
            code,length,offset=struct.unpack_from('<HHI',data,index+8*i)
            if not code:
                continue
            if offset+length>len(data):
                raise ValueError('Invalid Bigfont record')
            name,codes=shapefile.parse_shx_data_record(data[offset:offset+length])
            self.font.shapes[code]=shapefile.Symbol(code,length,name,codes)

    @lru_cache(maxsize=4096)
    def advance(self,char):
        try:
            encoded=char.encode('gbk')
            if len(encoded)!=2:
                return None
            code=int.from_bytes(encoded,'big')
            if code not in self.font.shapes:
                return None
            width=self.font.render_shape(code).end.x/self.font.above
            return width if 0<width<4 else None
        except (UnicodeError,ValueError,IndexError,RuntimeError):
            return None
