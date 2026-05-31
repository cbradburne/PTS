#include "WiFi.h"

void setup() {
  // Initialize serial communication at 115200 baud rate
  Serial.begin(115200);
  delay(1000); // Optional delay for stability

  // Set Wi-Fi to station mode and disconnect from any access point
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100); // Short delay to ensure Wi-Fi is in station mode

  // Print the MAC address of the ESP32
  Serial.println("ESP32 MAC Address: ");
  Serial.println(WiFi.macAddress());
}

void loop() {
  // Empty loop
}