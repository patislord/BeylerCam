package com.beyler.cam

import java.net.Inet4Address
import java.net.NetworkInterface

object NetUtils {
    data class LocalAddress(val interfaceName: String, val ip: String)

    /** Telefonun IPv4 adresleri; Wi-Fi önce, mobil veri en sonda. */
    fun localIpv4Addresses(): List<LocalAddress> = try {
        NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { ni ->
                ni.inetAddresses.toList()
                    .filterIsInstance<Inet4Address>()
                    .mapNotNull { addr -> addr.hostAddress?.let { LocalAddress(ni.name, it) } }
            }
            .sortedBy { priority(it.interfaceName) }
    } catch (e: Exception) {
        emptyList()
    }

    private fun priority(name: String): Int = when {
        name.startsWith("wlan") -> 0
        name.startsWith("swlan") || name.startsWith("ap") -> 1 // telefonun kendi hotspot'u
        name.startsWith("eth") -> 2
        name.startsWith("rndis") || name.startsWith("usb") -> 3
        else -> 4 // rmnet / ccmni: mobil veri
    }
}
