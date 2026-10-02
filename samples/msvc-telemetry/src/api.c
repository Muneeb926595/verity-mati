#include "telemetry.h"

/* Exported to the HMI: returns the latest temperature; FALSE while the link is not up. */
__declspec(dllexport) BOOL __stdcall Telemetry_GetTemperature(_Out_ double *temperatureC)
{
    if (temperatureC == NULL || g_linkState != LINK_UP) {
        return FALSE;
    }
    *temperatureC = g_temperatureC;
    return TRUE;
}
