void Serial4Data() {
  if (Serial4.available()) {                                    // If anything comes in Serial4 (pins 0 & 1)
    char g = Serial4.read();
    if (g == '#') {
      while (Serial4.available() < 1) {                         //  Wait for 1 byte to be available.
        delayMicroseconds(200);
      }
      char h = Serial4.read();
      if (h == 'Z') { Serial.println("~411"); }
      else if (h == 'X') { Serial.println("~421"); }
      else if (h == 'C') { Serial.println("~431"); }
      else if (h == 'V') { Serial.println("~441"); }
      else if (h == 'B') { Serial.println("~451"); }
      else if (h == 'N') { Serial.println("~461"); }
      else if (h == 'M') { Serial.println("~471"); }
      else if (h == '<') { Serial.println("~481"); }
      else if (h == '>') { Serial.println("~491"); }
      else if (h == '?') { Serial.println("~401"); }
      else if (h == 'A') { Serial.println("~412"); }
      else if (h == 'S') { Serial.println("~422"); }
      else if (h == 'D') { Serial.println("~432"); }
      else if (h == 'F') { Serial.println("~442"); }
      else if (h == 'G') { Serial.println("~452"); }
      else if (h == 'H') { Serial.println("~462"); }
      else if (h == 'J') { Serial.println("~472"); }
      else if (h == 'K') { Serial.println("~482"); }
      else if (h == 'L') { Serial.println("~492"); }
      else if (h == ':') { Serial.println("~402"); }
      else if (h == 'z') { Serial.println("~413"); }
      else if (h == 'x') { Serial.println("~423"); }
      else if (h == 'c') { Serial.println("~433"); }
      else if (h == 'v') { Serial.println("~443"); }
      else if (h == 'b') { Serial.println("~453"); }
      else if (h == 'n') { Serial.println("~463"); }
      else if (h == 'm') { Serial.println("~473"); }
      else if (h == ',') { Serial.println("~483"); }
      else if (h == '.') { Serial.println("~493"); }
      else if (h == '/') { Serial.println("~403"); }
      else if (h == 'p') { Serial.println("~414"); }
      else if (h == 'P') { Serial.println("~424"); }
      else if (h == 'O') { Serial.println("~415"); }
      else if (h == 'o') { Serial.println("~405"); }
      else if (h == 'a') { Serial.println("~400"); }
      else if (h == 's') { Serial.println("~&"); }
      else if (h == '0') {
        Serial.println("~100");
        Serial.println("~200");
        Serial.println("~300");
        Serial.println("~400");
        Serial.println("~500");
      }
      else if (h == 'd') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=f1");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'f') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=f2");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'g') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=f3");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'h') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=f4");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'j') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=F1");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'k') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=F2");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'l') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=F3");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == ';') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=F4");
        Serial.println(SerialCommandValueInt);
      }
      else if (h== 'q') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=f0");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'Q') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=F0");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 't') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=f5");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'w') {
        String stringText = Serial4.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=F5");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == '+') {
        Serial.println("=-4+");
        cam4Alive = true;
        previousCam4Alive = timeElapsed;
      }
      else if (h == 'i') {
        Serial.println("~405");
      }
      else if (h == 'I') {
        Serial.println("~415");
      }
    }
    else if (g == '^') {
      while (Serial4.available() < 2) {                        //  Wait for 1 byts to be available
        delayMicroseconds(200);
      }
      g = Serial4.read();
      if (g == '=') {
        cam4SlSpeed = Serial4.read();
        cam4SlSpeed -= 48;
        Serial.print("=4");
        Serial.println(cam4SlSpeed);
      }
      else if (g == '@') {
        cam4PTSpeed = Serial4.read();
        cam4PTSpeed -= 48;
        Serial.print("=@4");
        Serial.println(cam4PTSpeed);
      }
    }
    else if (g == 'W') { ; }
    else if (g == '\n') { ; }
    else if (g == '\r') { ; }
    else if (g == '?') { String readSerial4 = Serial4.readStringUntil('\n'); }

    else if (g == 4) {
      delay(1);
      while (Serial4.available() < 9) {                        //  Wait for 6 bytes to be available. Breaks after ~20ms if bytes are not received.
        delayMicroseconds(200);
      }
      char readSerialC4 = Serial4.read();
      readSerialC4 = Serial4.read();                          // Read Z
      readSerialC4 = Serial4.read();                          // Read X
      readSerialC4 = Serial4.read();                          // Read X
      readSerialC4 = Serial4.read();                          // Read Y
      readSerialC4 = Serial4.read();                          // Read Y
      readSerialC4 = Serial4.read();                          // Read W
      readSerialC4 = Serial4.read();                          // Read W
      readSerialC4 = Serial4.read();                          // Read Camera ID
    }

    else {
      inData4 = "\nCam4:\n";
      inData4 += g;
      delay(1);
      while (Serial4.available() > 0) {
        inData4 += Serial4.readStringUntil('\n');
        if (inData4 == "#$\r") {
          break;
        }
        Serial.println(inData4);
        inData4 = "";
        delay(2);
      }
    }
  }
}
