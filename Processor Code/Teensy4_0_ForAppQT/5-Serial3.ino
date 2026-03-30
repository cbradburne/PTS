void Serial3Data() {
  if (Serial3.available()) {                                    // If anything comes in Serial3 (pins 0 & 1)
    char g = Serial3.read();
    if (g == '#') {
      while (Serial3.available() < 1) {                         //  Wait for 1 byte to be available.
        delayMicroseconds(200);
      }
      char h = Serial3.read();
      if (h == 'Z') { Serial.println("~311"); }
      else if (h == 'X') { Serial.println("~321"); }
      else if (h == 'C') { Serial.println("~331"); }
      else if (h == 'V') { Serial.println("~341"); }
      else if (h == 'B') { Serial.println("~351"); }
      else if (h == 'N') { Serial.println("~361"); }
      else if (h == 'M') { Serial.println("~371"); }
      else if (h == '<') { Serial.println("~381"); }
      else if (h == '>') { Serial.println("~391"); }
      else if (h == '?') { Serial.println("~301"); }
      else if (h == 'A') { Serial.println("~312"); }
      else if (h == 'S') { Serial.println("~322"); }
      else if (h == 'D') { Serial.println("~332"); }
      else if (h == 'F') { Serial.println("~342"); }
      else if (h == 'G') { Serial.println("~352"); }
      else if (h == 'H') { Serial.println("~362"); }
      else if (h == 'J') { Serial.println("~372"); }
      else if (h == 'K') { Serial.println("~382"); }
      else if (h == 'L') { Serial.println("~392"); }
      else if (h == ':') { Serial.println("~302"); }
      else if (h == 'z') { Serial.println("~313"); }
      else if (h == 'x') { Serial.println("~323"); }
      else if (h == 'c') { Serial.println("~333"); }
      else if (h == 'v') { Serial.println("~343"); }
      else if (h == 'b') { Serial.println("~353"); }
      else if (h == 'n') { Serial.println("~363"); }
      else if (h == 'm') { Serial.println("~373"); }
      else if (h == ',') { Serial.println("~383"); }
      else if (h == '.') { Serial.println("~393"); }
      else if (h == '/') { Serial.println("~303"); }
      else if (h == 'p') { Serial.println("~314"); }
      else if (h == 'P') { Serial.println("~324"); }
      else if (h == 'O') { Serial.println("~315"); }
      else if (h == 'o') { Serial.println("~305"); }
      else if (h == 'a') { Serial.println("~300"); }
      else if (h == 's') { Serial.println("~@"); }
      else if (h == '0') {
        Serial.println("~100");
        Serial.println("~200");
        Serial.println("~300");
        Serial.println("~400");
        Serial.println("~500");
      }
      else if (h == 'd') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=d1");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'f') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=d2");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'g') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=d3");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'h') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=d4");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'j') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=D1");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'k') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=D2");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'l') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=D3");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == ';') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=D4");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'q') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=d0");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'Q') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=D0");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 't') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=d5");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'w') {
        String stringText = Serial3.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=D5");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == '+') {
        Serial.println("=-3+");
        cam3Alive = true;
        previousCam3Alive = timeElapsed;
      }
      else if (h == 'i') {
        Serial.println("~305");
      }
      else if (h == 'I') {
        Serial.println("~315");
      }
    }
    else if (g == '^') {
      while (Serial3.available() < 2) {                        //  Wait for 1 byts to be available
        delayMicroseconds(200);
      }
      g = Serial3.read();
      if (g == '=') {
        cam3SlSpeed = Serial3.read();
        cam3SlSpeed -= 48;
        Serial.print("=3");
        Serial.println(cam3SlSpeed);
      }
      else if (g == '@') {
        cam3PTSpeed = Serial3.read();
        cam3PTSpeed -= 48;
        Serial.print("=@3");
        Serial.println(cam3PTSpeed);
      }
    }
    else if (g == 'W') { ; }
    else if (g == '\n') { ; }
    else if (g == '\r') { ; }
    else if (g == '?') { String readSerial3 = Serial3.readStringUntil('\n'); }

    else if (g == 4) {
      delay(1);
      while (Serial3.available() < 9) {                        //  Wait for 6 bytes to be available. Breaks after ~20ms if bytes are not received.
        delayMicroseconds(200);
      }
      char readSerialC3 = Serial3.read();                     // Read Z
      readSerialC3 = Serial3.read();                          // Read Z
      readSerialC3 = Serial3.read();                          // Read X
      readSerialC3 = Serial3.read();                          // Read X
      readSerialC3 = Serial3.read();                          // Read Y
      readSerialC3 = Serial3.read();                          // Read Y
      readSerialC3 = Serial3.read();                          // Read W
      readSerialC3 = Serial3.read();                          // Read W
      readSerialC3 = Serial3.read();                          // Read Camera ID
    }

    else {
      inData3 = "\nCam3:\n";
      inData3 += g;
      delay(1);
      while (Serial3.available() > 0) {
        inData3 += Serial3.readStringUntil('\n');
        if (inData3 == "#$\r") {
          break;
        }
        Serial.println(inData3);
        inData3 = "";
        delay(2);
      }
    }
  }
}
