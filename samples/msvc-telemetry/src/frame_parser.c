#include "telemetry.h"

DWORD Crc32(_In_reads_bytes_(len) const BYTE *data, _In_ size_t len)
{
    DWORD crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}

/* Validates and decodes one frame. Returns FALSE (and counts the error) when the frame is short or corrupt. */
BOOL ParseFrame(_In_reads_bytes_(len) const BYTE *data, _In_ size_t len, _Out_ TELEMETRY_FRAME *out)
{
    if (len < sizeof(TELEMETRY_FRAME) || data[0] != FRAME_SYNC) {
        return FALSE;
    }
    memcpy(out, data, sizeof(TELEMETRY_FRAME));
#ifdef USE_LEGACY_PROTOCOL
    /* legacy firmware sends the temperature in tenths of a degree */
    out->temperatureCenti = (INT16)(out->temperatureCenti * 10);
#endif
    DWORD expected = Crc32(data, sizeof(TELEMETRY_FRAME) - sizeof(DWORD));
    if (out->crc != expected) {
        g_stats.crcErrors++;
        return FALSE;
    }
    return TRUE;
}
