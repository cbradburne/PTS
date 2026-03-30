void Serial1Data() {
  if (Serial1.available()) {                                    // If anything comes in Serial1 (pins 0 & 1)
    char c = Serial1.read();
    if (c == '#') {
      while (Serial1.available() < 1) {                         //  Wait for 1 byte to be available.
        delayMicroseconds(200);
      }
      char d = Serial1.read();
      if (d == 'Z') { Serial.println("~111"); }
      else if (d == 'X') { Serial.println("~121"); }
      else if (d == 'C') { Serial.println("~131"); }
      else if (d == 'V') { Serial.println("~141"); }
      else if (d == 'B') { Serial.println("~151"); }
      else if (d == 'N') { Serial.println("~161"); }
      else if (d == 'M') { Serial.println("~171"); }
      else if (d == '<') { Serial.println("~181"); }
      else if (d == '>') { Serial.println("~191"); }
      else if (d == '?') { Serial.println("~101"); }
      else if (d == 'A') { Serial.println("~112"); }
      else if (d == 'S') { Serial.println("~122"); }
      else if (d == 'D') { Serial.println("~132"); }
      else if (d == 'F') { Serial.println("~142"); }
      else if (d == 'G') { Serial.println("~152"); }
      else if (d == 'H') { Serial.println("~162"); }
      else if (d == 'J') { Serial.println("~172"); }
      else if (d == 'K') { Serial.println("~182"); }
      else if (d == 'L') { Serial.println("~192"); }
      else if (d == ':') { Serial.println("~102"); }
      else if (d == 'z') { Serial.println("~113"); }
      else if (d == 'x') { Serial.println("~123"); }
      else if (d == 'c') { Serial.println("~133"); }
      else if (d == 'v') { Serial.println("~143"); }
      else if (d == 'b') { Serial.println("~153"); }
      else if (d == 'n') { Serial.println("~163"); }
      else if (d == 'm') { Serial.println("~173"); }
      else if (d == ',') { Serial.println("~183"); }
      else if (d == '.') { Serial.println("~193"); }
      else if (d == '/') { Serial.println("~103"); }
      else if (d == 'p') { Serial.println("~114"); }
      else if (d == 'P') { Serial.println("~124"); }
      else if (d == 'O') { Serial.println("~115"); }
      else if (d == 'o') { Serial.println("~105"); }
      else if (d == 'a') { Serial.println("~100"); }
      else if (d == 's') { Serial.println("~?"); }
      else if (d == '0') {
        Serial.println("~100");
        Serial.println("~200");
        Serial.println("~300");
        Serial.println("~400");
        Serial.println("~500");
      }
      else if (d == 'd') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=a1");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 'f') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=a2");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 'g') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=a3");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 'h') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=a4");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 'j') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=A1");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 'k') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=A2");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 'l') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=A3");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == ';') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=A4");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 'q') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=a0");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 'Q') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=A0");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 't') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=a5");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == 'w') {
        String stringText = Serial1.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=A5");
        Serial.println(SerialCommandValueInt);
      }
      else if (d == '+') {
        Serial.println("=-1+");
        cam1Alive = true;
        previousCam1Alive = timeElapsed;
      }
      else if (d == 'i') {
        Serial.println("~105");
      }
      else if (d == 'I') {
        Serial.println("~115");
      }
    }
    else if (c == '^') {
      while (Serial1.available() < 2) {                        //  Wait for 1 byts to be available
        delayMicroseconds(200);
      }
      c = Serial1.read();
      if (c == '=') {
        cam1SlSpeed = Serial1.read();
        cam1SlSpeed -= 48;
        Serial.print("=1");
        Serial.println(cam1SlSpeed);
      }
      else if (c == '@') {
        cam1PTSpeed = Serial1.read();
        cam1PTSpeed -= 48;
        Serial.print("=@1");
        Serial.println(cam1PTSpeed);
      }
    }
    else if (c == 'W') { ; }
    else if (c == '\n') { ; }
    else if (c == '\r') { ; }
    else if (c == '?') { String readSerial1 = Serial1.readStringUntil('\n'); }

    else if (c == 4) {
      delay(1);
      while (Serial1.available() < 9) {                        //  Wait for 6 bytes to be available. Breaks after ~20ms if bytes are not received.
        delayMicroseconds(200);
      }
      char readSerialC1 = Serial1.read();                     // Read Z
      readSerialC1 = Serial1.read();                          // Read Z
      readSerialC1 = Serial1.read();                          // Read X
      readSerialC1 = Serial1.read();                          // Read X
      readSerialC1 = Serial1.read();                          // Read Y
      readSerialC1 = Serial1.read();                          // Read Y
      readSerialC1 = Serial1.read();                          // Read W
      readSerialC1 = Serial1.read();                          // Read W
      readSerialC1 = Serial1.read();                          // Read Camera ID
    }

    else {
      inData1 = "\nCam1:\n";
      inData1 += c;
      delay(1);
      while (Serial1.available() > 0) {
        inData1 += Serial1.readStringUntil('\n');
        if (inData1 == "#$\r") {
          break;
        }
        Serial.println(inData1);
        inData1 = "";
        delay(2);
      }
    }
  }
}
