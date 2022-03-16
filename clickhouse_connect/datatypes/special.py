import array
from uuid import UUID as PyUUID, SafeUUID
from struct import unpack_from as suf, pack as sp

from typing import Union, Any, Collection, Dict
from binascii import hexlify

from clickhouse_connect.datatypes.registry import ClickHouseType, get_from_name, TypeDef
from clickhouse_connect.driver.exceptions import NotSupportedError, DriverError
from clickhouse_connect.driver.rowbinary import read_leb128, to_leb128


class UUID(ClickHouseType):
    @staticmethod
    def _from_row_binary(source: bytearray, loc: int):
        int_high = int.from_bytes(source[loc:loc + 8], 'little')
        int_low = int.from_bytes(source[loc + 8:loc + 16], 'little')
        byte_value = int_high.to_bytes(8, 'big') + int_low.to_bytes(8, 'big')
        return PyUUID(bytes=byte_value), loc + 16

    @staticmethod
    def _to_row_binary(value: PyUUID, dest: bytearray):
        source = value.bytes
        bytes_high, bytes_low = bytearray(source[:8]), bytearray(source[8:])
        bytes_high.reverse()
        bytes_low.reverse()
        dest += bytes_high + bytes_low

    @staticmethod
    def from_native(source, loc, num_rows, must_swap):
        new_uuid = PyUUID.__new__
        unsafe_uuid = SafeUUID.unsafe
        oset = object.__setattr__
        v = suf(f'<{num_rows * 2}Q', source, loc)
        column = []
        app = column.append
        for ix in range(num_rows):
            s = ix << 1
            fast_uuid = new_uuid(PyUUID)
            oset(fast_uuid, 'int', v[s] << 64 | v[s + 1])
            oset(fast_uuid, 'is_safe', unsafe_uuid)
            app(fast_uuid)
        return column, loc + num_rows * 16


def _fixed_string_binary(value: bytearray):
    return bytes(value)


def _fixed_string_decode(cls, value: bytearray):
    try:
        return value.decode(cls._encoding)
    except UnicodeDecodeError:
        return cls._encode_error(value)


def _hex_string(cls, value: bytearray):
    return hexlify(value).decode('utf8')


class FixedString(ClickHouseType):
    __slots__ = 'size',
    _encoding = 'utf8'
    _transform = staticmethod(_fixed_string_binary)
    _encode_error = staticmethod(_fixed_string_binary)

    def __init__(self, type_def: TypeDef):
        super().__init__(type_def)
        self.size = type_def.values[0]
        self.name_suffix = f'({self.size})'

    def _from_row_binary(self, source: bytearray, loc: int):
        return self._transform(source[loc:loc + self.size]), loc + self.size

    def _to_row_binary(self, value: Union[str, bytes, bytearray], dest: bytearray):
        if isinstance(value, str):
            value = value.encode(self._encoding)
        dest += value

    def from_native(self, source, loc, num_rows, must_swap):
        sz = self.size
        column = tuple((bytes(source[loc + ix * sz:loc + ix * sz + sz]) for ix in range(num_rows)))
        return column, loc + sz * num_rows


def fixed_string_format(method: str, encoding: str, encoding_error: str):
    if method == 'binary':
        FixedString._transform = staticmethod(_fixed_string_binary)
    elif method == 'decode':
        FixedString._encoding = encoding
        FixedString._transform = classmethod(_fixed_string_decode)
        if encoding_error == 'hex':
            FixedString._encode_error = classmethod(_hex_string)
        else:
            FixedString._encode_error = classmethod(lambda cls: '<binary data>')
    elif method == 'hex':
        FixedString._transform = staticmethod(_hex_string)


class Nothing(ClickHouseType):
    @staticmethod
    def _from_row_binary(self, source: bytes, loc: int):
        return None, loc

    def _to_row_binary(self, value: Any, dest: bytearray):
        pass


class Array(ClickHouseType):
    __slots__ = 'element_type',

    def __init__(self, type_def: TypeDef):
        super().__init__(type_def)
        self.element_type: ClickHouseType = get_from_name(type_def.values[0])
        if isinstance(self.element_type, Array):
            raise DriverError("Nested arrays not supported")
        self.name_suffix = type_def.arg_str

    def _from_row_binary(self, source: bytearray, loc: int):
        size, loc = read_leb128(source, loc)
        values = []
        for x in range(size):
            value, loc = self.element_type.from_row_binary(source, loc)
            values.append(value)
        return values, loc

    def from_native(self, source: bytearray, loc: int, num_rows: int, must_swap: bool):
        conv = self.element_type.from_native
        conv_py = self.element_type.to_python
        nullable = self.element_type.nullable
        offsets = array.array('Q')
        sz = num_rows * 8
        offsets.frombytes(source[loc: loc + sz])
        loc += sz
        if must_swap:
            offsets.byteswap()
        column = []
        app = column.append
        last = 0
        for offset in offsets:
            cnt = offset - last
            last = offset
            val_list, loc = conv(source, loc, cnt, must_swap)
            if conv_py:
                val_list = conv_py(val_list)
            app(val_list)
        return column, loc

    def _to_row_binary(self, values: Collection[Any], dest: bytearray):
        dest += to_leb128(len(values))
        conv = self.element_type.to_row_binary
        for value in values:
            conv(value, dest)


class Tuple(ClickHouseType):
    _slots = 'from_rb_funcs', 'to_rb_funcs'

    def __init__(self, type_def: TypeDef):
        super().__init__(type_def)
        self.from_rb_funcs = tuple([get_from_name(name).from_row_binary for name in type_def.values])
        self.to_rb_funcs = tuple([get_from_name(name).to_row_binary for name in type_def.values])
        self.name_suffix = type_def.arg_str

    def _from_row_binary(self, source: bytes, loc: int):
        values = []
        for conv in self.from_rb_funcs:
            value, loc = conv(source, loc)
            values.append(value)
        return tuple(values), loc

    def _to_row_binary(self, values: Collection, dest: bytearray):
        for value, conv in zip(values, self.to_rb_funcs):
            conv(value, dest)


class Map(ClickHouseType):
    _slots = 'key_from_rb', 'key_to_rb', 'value_from_rb', 'value_to_rb'

    def __init__(self, type_def: TypeDef):
        super().__init__(type_def)
        ch_type = get_from_name(type_def.values[0])
        self.key_from_rb, self.key_to_rb = ch_type.from_row_binary, ch_type.to_row_binary
        ch_type = get_from_name(type_def.values[1])
        self.value_from_rb, self.value_to_rb = ch_type.from_row_binary, ch_type.to_row_binary
        self.name_suffix = type_def.arg_str

    def _from_row_binary(self, source, loc):
        size, loc = read_leb128(source, loc)
        values = {}
        key_from = self.key_from_rb
        value_from = self.value_from_rb
        for x in range(size):
            key, loc = key_from(source, loc)
            value, loc = value_from(source, loc)
            values[key] = value
        return values, loc

    def _to_row_binary(self, values: Dict) -> bytearray:
        key_to = self.key_to_rb
        value_to = self.value_to_rb
        ret = bytearray()
        for key, value in values.items():
            ret.extend(key_to(key))
            ret.extend(value_to(key))
        return ret


class AggregateFunction(ClickHouseType):
    def __init__(self, type_def: TypeDef):
        super().__init__(type_def)
        self.name_suffix = type_def.arg_str

    def _from_row_binary(self, source, loc):
        raise NotSupportedError("Aggregate function deserialization not supported")

    def _to_row_binary(self, value: Any) -> bytes:
        raise NotSupportedError("Aggregate function serialization not supported")


class SimpleAggregateFunction(ClickHouseType):
    _slots = 'element_type',

    def __init__(self, type_def: TypeDef):
        super().__init__(type_def)
        self.element_type: ClickHouseType = get_from_name(type_def.values[1])
        self.name_suffix = type_def.arg_str

    def _from_row_binary(self, source, loc):
        return self.element_type.from_row_binary(source, loc)

    def _to_row_binary(self, value: Any) -> bytes:
        return self.element_type.to_row_binary(value)
