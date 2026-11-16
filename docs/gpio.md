# Raspberry Pi shutdown button and OLED

Both features start disabled. Set them up in **Settings → System → GPIO Accessories**. The instructions apply to a Raspberry Pi with its standard 40-pin header. Shut the Pi down and disconnect its power before wiring. Confirm pin 1 using the board marking or the Raspberry Pi pinout.

| Accessory | Signal | Pi GPIO | Physical pin |
| --- | --- | --- | --- |
| Momentary button | One switch contact | BCM GPIO17 by default | 11 |
| Momentary button | Other switch contact | Ground | 9 |
| SSD1306 OLED | VCC | 3.3 V | 1 |
| SSD1306 OLED | GND | Ground | 6 |
| SSD1306 OLED | SDA | BCM GPIO2 | 3 |
| SSD1306 OLED | SCL | BCM GPIO3 | 5 |

Use a normally open momentary switch. On a four-leg tactile switch, use contacts that only connect while pressed. The Pi supplies the internal pull-up. Choose a different BCM pin only if it is free, and move the switch lead to that GPIO's physical pin. GPIO2 and GPIO3 are reserved for the OLED bus.

Use a four-wire, 3.3 V-compatible SSD1306 I²C display with a 128×32 panel. Check the module's printed pin labels, because their order varies. Power it from 3.3 V so the signal lines stay at 3.3 V.

After wiring and restoring power, enable I²C in **Interface Options** with `sudo raspi-config` and reboot if prompted. In PrepperCache, enable the OLED and select its address, usually **0x3C**. The screen keeps the active IPv4 address on line one and cycles through content activity, map activity, and free system-drive space on line two.

Enable the shutdown button after confirming its wiring and BCM pin. Release it once after enabling, then hold it for two seconds to request a normal Linux power-off. Wait for shutdown to complete before removing power. The switch does not turn the Pi back on.

On an existing installation, install the new service with `sudo bash scripts/install_gpio_service.sh`. See recent hardware messages with `sudo journalctl -u preppercache-gpio -n 30`.

- [Raspberry Pi pinout](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio)
- [Raspberry Pi interface configuration](https://www.raspberrypi.com/documentation/computers/configuration.html)
- [GPIO Zero button wiring](https://gpiozero.readthedocs.io/en/stable/api_input.html#button)
- [Luma OLED drivers](https://github.com/rm-hull/luma.oled)
