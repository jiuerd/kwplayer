# Copyright 2006 Joe Wreschnig
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# $Id: mp4.py 4233 2007-12-28 07:24:59Z luks $

"""Read and write MPEG-4 audio files with iTunes metadata.

This module will read MPEG-4 audio information and metadata,
as found in Apple's MP4 (aka M4A, M4B, M4P) files.

There is no official specification for this format. The source code
for TagLib, FAAD, and various MPEG specifications at
http://developer.apple.com/documentation/QuickTime/QTFF/,
http://www.geocities.com/xhelmboyx/quicktime/formats/mp4-layout.txt,
http://standards.iso.org/ittf/PubliclyAvailableStandards/c041828_ISO_IEC_14496-12_2005(E).zip,
and http://wiki.multimedia.cx/index.php?title=Apple_QuickTime were all
consulted.
"""

import struct
import sys

from mutagenx import FileType, Metadata
from mutagenx._constants import GENRES
from mutagenx._util import cdata, insert_bytes, delete_bytes, DictProxy, utf8

class error(IOError): pass
class MP4MetadataError(error): pass
class MP4StreamInfoError(error): pass
class MP4MetadataValueError(ValueError, MP4MetadataError): pass

# This is not an exhaustive list of container atoms, but just the
# ones this module needs to peek inside.
_CONTAINERS = [b"moov", b"udta", b"trak", b"mdia", b"meta", b"ilst",
               b"stbl", b"minf", b"moof", b"traf"]
_SKIP_SIZE = { b"meta": 4 }

__all__ = ['MP4', 'Open', 'delete', 'MP4Cover', 'MP4FreeForm']


class MP4Cover(bytes):
    """A cover artwork.

    Attributes:
    imageformat -- format of the image (either FORMAT_JPEG or FORMAT_PNG)
    """
    FORMAT_JPEG = 0x0D
    FORMAT_PNG = 0x0E

    def __new__(cls, data, imageformat=None):
        self = bytes.__new__(cls, data)

        if imageformat is None:
            imageformat = MP4Cover.FORMAT_JPEG
        self.imageformat = imageformat

        try:
            self.format
        except AttributeError:
            self.format = imageformat
        return self

class MP4FreeForm(bytes):
    """A freeform value.

    Attributes:
    dataformat -- format of the data (either FORMAT_TEXT or FORMAT_DATA)
    """
    FORMAT_DATA = 0x0
    FORMAT_TEXT = 0x1

    def __new__(cls, data, dataformat=None):
        self = bytes.__new__(cls, data)
        if dataformat is None:
            dataformat = MP4FreeForm.FORMAT_TEXT
        self.dataformat = dataformat
        return self

class Atom(object):
    """An individual atom.

    Attributes:
    children -- list child atoms (or None for non-container atoms)
    length -- length of this atom, including length and name
    name -- four byte name of the atom, as bytes
    offset -- location in the constructor-given fileobj of this atom

    This structure should only be used internally by Mutagen.
    """

    children = None

    def __init__(self, fileobj, level=0):
        self.offset = fileobj.tell()
        self.length, self.name = struct.unpack(">I4s", fileobj.read(8))

        if self.length == 1:
            self.length = struct.unpack(">Q", fileobj.read(8))[0]

        elif self.length == 0:
            if level != 0:
                raise MP4MetadataError(
                    "only a top-level atom can have zero length")
            # Only the last atom is supposed to have a zero-length, meaning it
            # extends to the end of file.
            fileobj.seek(0, 2)
            self.length = fileobj.tell() - self.offset
            fileobj.seek(self.offset + 8, 0)
        elif self.length < 8:
            raise MP4MetadataError(
                "atom length can only be 0, 1 or 8 and higher")

        if self.name in _CONTAINERS:
            self.children = []
            fileobj.seek(_SKIP_SIZE.get(self.name, 0), 1)
            while fileobj.tell() < self.offset + self.length:
                self.children.append(Atom(fileobj, level + 1))
        else:
            fileobj.seek(self.offset + self.length, 0)

    # Takes two bytes arguments
    @staticmethod
    def render(name, data):
        """Render raw atom data."""
        # this raises OverflowError if Py_ssize_t can't handle the atom data
        size = len(data) + 8
        if size <= 0xFFFFFFFF:
            return struct.pack(">I4s", size, name) + data
        else:
            return struct.pack(">I4sQ", 1, name, size + 8) + data

    def findall(self, name, recursive=False):
        """Recursively find all child atoms by specified name."""
        if self.children is not None:
            for child in self.children:
                if child.name == name:
                    yield child
                if recursive:
                    for atom in child.findall(name, True):
                        yield atom

    def __getitem__(self, remaining):
        """Look up a child atom, potentially recursively.

        e.g. atom['udta', 'meta'] => <Atom name='meta' ...>
        """
        if not remaining:
            return self
        elif self.children is None:
            raise KeyError("{!r} is not a container".format(self.name))
        for child in self.children:
            if child.name == remaining[0]:
                return child[remaining[1:]]
        else:
            raise KeyError("{!r} not found".format(remaining[0]))

    def __repr__(self):
        cls = self.__class__.__name__
        if self.children is None:
            return "<{} name={!r} length={!r} offset={!r}>".format(
                cls, self.name, self.length, self.offset)
        else:
            children = "\n".join([" " + line for child in self.children
                                  for line in repr(child).splitlines()])
            return "<{} name={!r} length={!r} offset={!r}\n{}>".format(
                cls, self.name, self.length, self.offset, children)

class Atoms(object):
    """Root atoms in a given file.

    Attributes:
    atoms -- a list of top-level atoms as Atom objects

    This structure should only be used internally by Mutagen.
    """
    def __init__(self, fileobj):
        self.atoms = []
        fileobj.seek(0, 2)
        end = fileobj.tell()
        fileobj.seek(0)
        while fileobj.tell() + 8 <= end:
            self.atoms.append(Atom(fileobj))

    def path(self, *names):
        """Look up and return the complete path of an atom.

        For example, atoms.path('moov', 'udta', 'meta') will return a
        list of three atoms, corresponding to the moov, udta, and meta
        atoms.
        """
        path = [self]
        for name in names:
            path.append(path[-1][name,])
        return path[1:]

    def __getitem__(self, names):
        """Look up a child atom.

        'names' may be a list of atoms (['moov', 'udta']) or a string
        specifying the complete path ('moov.udta').
        """
        if isinstance(names, bytes):
            names = names.split(b".")
        for child in self.atoms:
            if child.name == names[0]:
                return child[names[1:]]
        else:
            raise KeyError("{!r} not found".format(names[0]))

    def __repr__(self):
        return "\n".join(repr(child) for child in self.atoms)


class MP4Tags(DictProxy, Metadata):
    """Dictionary containing Apple iTunes metadata list key/values.

    Keys are four byte identifiers, except for freeform ('----')
    keys. Values are usually unicode strings, but some atoms have a
    special structure:

    Text values (multiple values per key are supported):
        '\xa9nam' -- track title
        '\xa9alb' -- album
        '\xa9ART' -- artist
        'aART' -- album artist
        '\xa9wrt' -- composer
        '\xa9day' -- year
        '\xa9cmt' -- comment
        'desc' -- description (usually used in podcasts)
        'purd' -- purchase date
        '\xa9grp' -- grouping
        '\xa9gen' -- genre
        '\xa9lyr' -- lyrics
        'purl' -- podcast URL
        'egid' -- podcast episode GUID
        'catg' -- podcast category
        'keyw' -- podcast keywords
        '\xa9too' -- encoded by
        'cprt' -- copyright
        'soal' -- album sort order
        'soaa' -- album artist sort order
        'soar' -- artist sort order
        'sonm' -- title sort order
        'soco' -- composer sort order
        'sosn' -- show sort order
        'tvsh' -- show name

    Boolean values:
        'cpil' -- part of a compilation
        'pgap' -- part of a gapless album
        'pcst' -- podcast (iTunes reads this only on import)

    Tuples of ints (multiple values per key are supported):
        'trkn' -- track number, total tracks
        'disk' -- disc number, total discs

    Others:
        'tmpo' -- tempo/BPM, 16 bit int
        'covr' -- cover artwork, list of MP4Cover objects (which are
                  tagged strs)
        'gnre' -- ID3v1 genre. Not supported, use '\xa9gen' instead.

    The freeform '----' frames use a key in the format '----:mean:name'
    where 'mean' is usually 'com.apple.iTunes' and 'name' is a unique
    identifier for this frame. The value is a str, but is probably
    text that can be decoded as UTF-8. Multiple values per key are
    supported.

    MP4 tag data cannot exist outside of the structure of an MP4 file,
    so this class should not be manually instantiated.

    Unknown non-text tags are removed.
    """

    def load(self, atoms, fileobj):
        try:
            ilst = atoms[b"moov.udta.meta.ilst"]
        except KeyError as key:
            raise MP4MetadataError(key)
        for atom in ilst.children:
            fileobj.seek(atom.offset + 8)
            data = fileobj.read(atom.length - 8)
            info = self.__atoms.get(atom.name, (type(self).__parse_text, None))
            info[0](self, atom, data, *info[2:])

    @staticmethod
    def __get_sort_stats(item):
        (key, v) = item
        # iTunes always writes the tags in order of "relevance", try
        # to copy it as closely as possible.
        order = [b"\xa9nam", b"\xa9ART", b"\xa9wrt", b"\xa9alb",
                 b"\xa9gen", b"gnre", b"trkn", b"disk",
                 b"\xa9day", b"cpil", b"pgap", b"pcst", b"tmpo",
                 b"\xa9too", b"----", b"covr", b"\xa9lyr"]
        order = dict(zip(order, range(len(order))))
        last = len(order)
        # If there's no key-based way to distinguish, order by length.
        # If there's still no way, go by string comparison on the
        # values, so we at least have something determinstic.

        try:
            length = len(v)
        except TypeError:
            length = 0

        return (order.get(key[:4], last), length, v)


    def save(self, filename):
        """Save the metadata to the given filename."""
        values = []
        items = sorted(self.items(), key=MP4Tags.__get_sort_stats )
        for key, value in items:
            info = self.__atoms.get(key[:4], (None, type(self).__render_text))
            try:
                values.append(info[1](self, key, value, *info[2:]))
            except (TypeError, ValueError) as s:
                raise MP4MetadataValueError(s).with_traceback(sys.exc_info()[2])
        data = Atom.render(b"ilst", b"".join(values))

        # Find the old atoms.
        fileobj = open(filename, "rb+")
        try:
            atoms = Atoms(fileobj)
            try:
                path = atoms.path(b"moov", b"udta", b"meta", b"ilst")
            except KeyError:
                self.__save_new(fileobj, atoms, data)
            else:
                self.__save_existing(fileobj, atoms, path, data)
        finally:
            fileobj.close()

    def __pad_ilst(self, data, length=None):
        if length is None:
            length = ((len(data) + 1023) & ~1023) - len(data)
        return Atom.render(b"free", b"\x00" * length)

    def __save_new(self, fileobj, atoms, ilst):
        hdlr = Atom.render(b"hdlr", b"\x00" * 8 + b"mdirappl" + b"\x00" * 9)
        meta = Atom.render(
            b"meta", b"\x00\x00\x00\x00" + hdlr + ilst + self.__pad_ilst(ilst))
        try:
            path = atoms.path(b"moov", b"udta")
        except KeyError:
            # moov.udta not found -- create one
            path = atoms.path(b"moov")
            meta = Atom.render(b"udta", meta)
        offset = path[-1].offset + 8
        insert_bytes(fileobj, len(meta), offset)
        fileobj.seek(offset)
        fileobj.write(meta)
        self.__update_parents(fileobj, path, len(meta))
        self.__update_offsets(fileobj, atoms, len(meta), offset)

    def __save_existing(self, fileobj, atoms, path, data):
        # Replace the old ilst atom.
        ilst = path.pop()
        offset = ilst.offset
        length = ilst.length

        # Check for padding "free" atoms
        meta = path[-1]
        index = meta.children.index(ilst)
        try:
            prev = meta.children[index-1]
            if prev.name == b"free":
                offset = prev.offset
                length += prev.length
        except IndexError:
            pass
        try:
            next = meta.children[index+1]
            if next.name == b"free":
                length += next.length
        except IndexError:
            pass

        delta = len(data) - length
        if delta > 0 or (delta < 0 and delta > -8):
            data += self.__pad_ilst(data)
            delta = len(data) - length
            insert_bytes(fileobj, delta, offset)
        elif delta < 0:
            data += self.__pad_ilst(data, -delta - 8)
            delta = 0

        fileobj.seek(offset)
        fileobj.write(data)
        self.__update_parents(fileobj, path, delta)
        self.__update_offsets(fileobj, atoms, delta, offset)

    def __update_parents(self, fileobj, path, delta):
        """Update all parent atoms with the new size."""
        for atom in path:
            fileobj.seek(atom.offset)
            size = cdata.uint_be(fileobj.read(4))
            if size == 1: # 64bit
                # skip name (4B) and read size (8B)
                size = cdata.ulonglong_be(fileobj.read(12)[4:])
                fileobj.seek(atom.offset + 8)
                fileobj.write(cdata.to_ulonglong_be(size + delta))
            else: # 32bit
                fileobj.seek(atom.offset)
                fileobj.write(cdata.to_uint_be(size + delta))

    def __update_offset_table(self, fileobj, fmt, atom, delta, offset):
        """Update offset table in the specified atom."""
        if atom.offset > offset:
            atom.offset += delta
        fileobj.seek(atom.offset + 12)
        data = fileobj.read(atom.length - 12)
        fmt = fmt.format(cdata.uint_be(data[:4]))
        offsets = struct.unpack(fmt, data[4:])
        offsets = [o + (0, delta)[offset < o] for o in offsets]
        fileobj.seek(atom.offset + 16)
        fileobj.write(struct.pack(fmt, *offsets))

    def __update_tfhd(self, fileobj, atom, delta, offset):
        if atom.offset > offset:
            atom.offset += delta
        fileobj.seek(atom.offset + 9)
        data = fileobj.read(atom.length - 9)
        flags = cdata.uint_be(b"\x00" + data[:3])
        if flags & 1:
            o = cdata.ulonglong_be(data[7:15])
            if o > offset:
                o += delta
            fileobj.seek(atom.offset + 16)
            fileobj.write(cdata.to_ulonglong_be(o))

    def __update_offsets(self, fileobj, atoms, delta, offset):
        """Update offset tables in all 'stco' and 'co64' atoms."""
        if delta == 0:
            return
        moov = atoms[b"moov"]
        for atom in moov.findall(b'stco', True):
            self.__update_offset_table(fileobj, ">{:d}I", atom, delta, offset)
        for atom in moov.findall(b'co64', True):
            self.__update_offset_table(fileobj, ">{:d}Q", atom, delta, offset)
        try:
            for atom in atoms[b"moof"].findall('tfhd', True):
                self.__update_tfhd(fileobj, atom, delta, offset)
        except KeyError:
            pass

    def __parse_data(self, atom, data):
        pos = 0
        while pos < atom.length - 8:
            length, name, flags = struct.unpack(">I4sI", data[pos:pos+12])
            if name != b"data":
                raise MP4MetadataError(
                    "unexpected atom {!r} inside {!r}".format(name, atom.name))
            yield flags, data[pos+16:pos+length]
            pos += length

    def __render_data(self, key, flags, value):
        return Atom.render(key, b"".join(
            Atom.render(b"data", struct.pack(">2I", flags, 0) + data)
            for data in value))

    def __parse_freeform(self, atom, data):
        length = cdata.uint_be(data[:4])
        mean = data[12:length]
        pos = length
        length = cdata.uint_be(data[pos:pos+4])
        name = data[pos+12:pos+length]
        pos += length
        value = []
        while pos < atom.length - 8:
            length, atom_name = struct.unpack(">I4s", data[pos:pos+8])
            if atom_name != b"data":
                raise MP4MetadataError(
                    "unexpected atom {!r} inside {!r}".format(atom_name, atom.name))

            version = data[pos+8]
            if version != 0:
                raise MP4MetadataError("Unsupported version: {!r}".format(version))

            flags = struct.unpack(">I", b"\x00" + data[pos+9:pos+12])[0]
            value.append(MP4FreeForm(data[pos+16:pos+length],
                                     dataformat=flags))
            pos += length
        if value:
            self[atom.name + b":" + mean + b":" + name] = value

    def __render_freeform(self, key, value):
        dummy, mean, name = key.split(b":", 2)
        mean = struct.pack(">I4sI", len(mean) + 12, b"mean", 0) + mean
        name = struct.pack(">I4sI", len(name) + 12, b"name", 0) + name
        if isinstance(value, bytes):
            value = [value]
        data = b""
        for v in value:
            flags = MP4FreeForm.FORMAT_TEXT
            if isinstance(v, MP4FreeForm):
                flags = v.dataformat
            data += struct.pack(">I4s2I", len(v) + 16, b"data", flags, 0)
            data += v
        return Atom.render(b"----", mean + name + data)

    def __parse_pair(self, atom, data):
        self[atom.name] = [struct.unpack(">2H", data[2:6]) for
                           flags, data in self.__parse_data(atom, data)]
    def __render_pair(self, key, value):
        data = []
        for (track, total) in value:
            if 0 <= track < 1 << 16 and 0 <= total < 1 << 16:
                data.append(struct.pack(">4H", 0, track, total, 0))
            else:
                raise MP4MetadataValueError(
                    "invalid numeric pair {!r}".format((track, total),))
        return self.__render_data(key, 0, data)

    def __render_pair_no_trailing(self, key, value):
        data = []
        for (track, total) in value:
            if 0 <= track < 1 << 16 and 0 <= total < 1 << 16:
                data.append(struct.pack(">3H", 0, track, total))
            else:
                raise MP4MetadataValueError(
                    "invalid numeric pair {!r}".format((track, total),))
        return self.__render_data(key, 0, data)

    def __parse_genre(self, atom, data):
        # Translate to a freeform genre.
        genre = cdata.short_be(data[16:18])
        if b"\xa9gen" not in self:
            try:
                self[b"\xa9gen"] = [GENRES[genre - 1]]
            except IndexError:
                pass

    def __parse_tempo(self, atom, data):
        self[atom.name] = [cdata.ushort_be(value[1]) for
                           value in self.__parse_data(atom, data)]

    def __render_tempo(self, key, value):
        try:
            if not isinstance(value, list):
                raise TypeError

            if len(value) == 0:
                return self.__render_data(key, 0x15, "")

            if min(value) < 0 or max(value) >= 2**16:
                raise MP4MetadataValueError(
                    "invalid 16 bit integers: {!r}".format(value))
        except TypeError:
            raise MP4MetadataValueError(
                "tmpo must be a list of 16 bit integers")

        values = [cdata.to_ushort_be(v) for v in value]
        return self.__render_data(key, 0x15, values)

    def __parse_bool(self, atom, data):
        try:
            self[atom.name] = bool(data[16])
        except IndexError:
            self[atom.name] = False

    def __render_bool(self, key, value):
        return self.__render_data(key, 0x15, [bytes((int(bool(value)),))])

    def __parse_cover(self, atom, data):
        self[atom.name] = []
        pos = 0
        while pos < atom.length - 8:
            length, name, imageformat = struct.unpack(">I4sI", data[pos:pos+12])
            if name != b"data":
                if name == b"name":
                    pos += length
                    continue
                raise MP4MetadataError(
                    "unexpected atom {!r} inside 'covr'".format(name))
            if imageformat not in (MP4Cover.FORMAT_JPEG, MP4Cover.FORMAT_PNG):
                imageformat = MP4Cover.FORMAT_JPEG
            cover = MP4Cover(data[pos+16:pos+length], imageformat)
            self[atom.name].append(
                MP4Cover(data[pos+16:pos+length], imageformat))
            pos += length

    def __render_cover(self, key, value):
        atom_data = []
        for cover in value:
            try:
                imageformat = cover.imageformat
            except AttributeError:
                imageformat = MP4Cover.FORMAT_JPEG
            atom_data.append(
                Atom.render(b"data", struct.pack(">2I", imageformat, 0) + cover))
        return Atom.render(key, b"".join(atom_data))

    def __parse_text(self, atom, data, expected_flags=1):
        value = [text.decode('utf-8', 'replace') for flags, text
                 in self.__parse_data(atom, data)
                 if flags == expected_flags]
        if value:
            self[atom.name] = value

    def __render_text(self, key, value, flags=1):
        if isinstance(value, str) or isinstance(value, bytes):
            value = [value]
        return self.__render_data(
            key, flags, [utf8(v) for v in value])

    def delete(self, filename):
        self.clear()
        self.save(filename)

    __atoms = {
        b"----": (__parse_freeform, __render_freeform),
        b"trkn": (__parse_pair, __render_pair),
        b"disk": (__parse_pair, __render_pair_no_trailing),
        b"gnre": (__parse_genre, None),
        b"tmpo": (__parse_tempo, __render_tempo),
        b"cpil": (__parse_bool, __render_bool),
        b"pgap": (__parse_bool, __render_bool),
        b"pcst": (__parse_bool, __render_bool),
        b"covr": (__parse_cover, __render_cover),
        b"purl": (__parse_text, __render_text, 0),
        b"egid": (__parse_text, __render_text, 0),
        }

    def pprint(self):
        values = []
        for key, value in self.items():
            if key == b"covr":
                values.append("{}={}".format(key, ", ".join(
                    "[{} bytes of data]".format(len(data)) for data in value)))
            elif isinstance(value, list):
                values.append("{}={}".format(key, " / ".join(str(v) for v in value)))
            else:
                values.append("{}={}".format(key, value))
        return "\n".join(values)


class MP4Info(object):
    """MPEG-4 stream information.

    Attributes:
    bitrate -- bitrate in bits per second, as an int
    length -- file length in seconds, as a float
    channels -- number of audio channels
    sample_rate -- audio sampling rate in Hz
    bits_per_sample -- bits per sample
    """

    bitrate = 0
    channels = 0
    sample_rate = 0
    bits_per_sample = 0

    def __init__(self, atoms, fileobj):
        for trak in list(atoms[b"moov"].findall(b"trak")):
            hdlr = trak[b"mdia", b"hdlr"]
            fileobj.seek(hdlr.offset)
            data = fileobj.read(hdlr.length)
            if data[16:20] == b"soun":
                break
        else:
            raise MP4StreamInfoError("track has no audio data")

        mdhd = trak[b"mdia", b"mdhd"]
        fileobj.seek(mdhd.offset)
        data = fileobj.read(mdhd.length)
        if data[8] == 0:
            offset = 20
            fmt = ">2I"
        else:
            offset = 28
            fmt = ">IQ"
        end = offset + struct.calcsize(fmt)
        unit, length = struct.unpack(fmt, data[offset:end])
        self.length = length / unit

        try:
            atom = trak[b"mdia", b"minf", b"stbl", b"stsd"]
            fileobj.seek(atom.offset)
            data = fileobj.read(atom.length)
            if data[20:24] == b"mp4a":
                length = cdata.uint_be(data[16:20])
                (self.channels, self.bits_per_sample, _,
                 self.sample_rate) = struct.unpack(">3HI", data[40:50])
                # ES descriptor type
                if data[56:60] == b"esds" and data[64] == 0x03:
                    pos = 65
                    # skip extended descriptor type tag, length, ES ID
                    # and stream priority
                    if data[pos:pos+3] == b"\x80\x80\x80":
                        pos += 3
                    pos += 4
                    # decoder config descriptor type
                    if data[pos] == 0x04:
                        pos += 1
                        # skip extended descriptor type tag, length,
                        # object type ID, stream type, buffer size
                        # and maximum bitrate
                        if data[pos:pos+3] == b"\x80\x80\x80":
                            pos += 3
                        pos += 10
                        # average bitrate
                        self.bitrate = cdata.uint_be(data[pos:pos+4])
        except (ValueError, KeyError):
            # stsd atoms are optional
            pass

    def pprint(self):
        return "MPEG-4 audio, {:.2f} seconds, {} bps".format(
            self.length, self.bitrate)

class MP4(FileType):
    """An MPEG-4 audio file, probably containing AAC.

    If more than one track is present in the file, the first is used.
    Only audio ('soun') tracks will be read.
    """

    MP4Tags = MP4Tags

    _mimes = ["audio/mp4", "audio/x-m4a", "audio/mpeg4", "audio/aac"]

    def load(self, filename):
        self.filename = filename
        fileobj = open(filename, "rb")
        try:
            atoms = Atoms(fileobj)
            try:
                self.info = MP4Info(atoms, fileobj)
            except Exception as err:
                raise MP4StreamInfoError(err).with_traceback(sys.exc_info()[2])
            try:
                self.tags = self.MP4Tags(atoms, fileobj)
            except MP4MetadataError:
                self.tags = None
            except Exception as err:
                raise MP4MetadataError(err).with_traceback(sys.exc_info()[2])
        finally:
            fileobj.close()

    def add_tags(self):
        self.tags = self.MP4Tags()

    @staticmethod
    def score(filename, fileobj, header_data):
        return (b"ftyp" in header_data) + (b"mp4" in header_data)

Open = MP4

def delete(filename):
    """Remove tags from a file."""
    MP4(filename).delete()
