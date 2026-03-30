void USBSerialData() {
  if (Serial.available()) {  // If anything comes in Serial (USB),
    char instruction = Serial.read();
    if (instruction == 4) {
      //delay(1);
      while (Serial.available() < 9) {  //  Wait for 6 bytes to be available. Breaks after ~20ms if bytes are not received.
        //delayMicroseconds(200);
        ;
      }
      short ZShortPC = (Serial.read() << 8) + Serial.read();
      short XShortPC = (Serial.read() << 8) + Serial.read();
      short YShortPC = (Serial.read() << 8) + Serial.read();
      short WShortPC = (Serial.read() << 8) + Serial.read();

      whichSerialCam = Serial.read();

      shortVals[0] = ZShortPC;
      shortVals[1] = XShortPC;
      shortVals[2] = YShortPC;
      shortVals[3] = WShortPC;

      sendSliderPanTiltStepSpeed(4, shortVals, whichSerialCam);

      //Serial.println("Joystick");
    } else if (instruction == '&') {
      while (Serial.available() < 1) {  //  Wait for 1 byte to be available.
        //delayMicroseconds(1);
        ;
      }
      instruction = Serial.read();
      if (instruction == '1' || instruction == '2' || instruction == '3' || instruction == '4' || instruction == '5') {
        char camNumInst = instruction;
        while (Serial.available() < 1) {  //  Wait for 1 byte to be available.
          //delayMicroseconds(1);
          ;
        }
        instruction = Serial.read();

        /*
        Z = Save Pos1
        X = Save Pos2
        C = Save Pos3
        V = Save Pos4
        B = Save Pos5
        N = Save Pos6
        M = Save Pos7
        < = Save Pos8
        > = Save Pos9
        ? = Save Pos10

        z = Move Pos1
        x = Move Pos2
        c = Move Pos3
        v = Move Pos4
        b = Move Pos5
        n = Move Pos6
        m = Move Pos7
        , = Move Pos8
        . = Move Pos9
        / = Move Pos10

        y = Move to Pos1 SLIDER ONLY
        Y = Move to Pos10 SLIDER ONLY

        a(+) = Zoom out
        A(+) = Zoom in
        q = Zoom Stop

        d = Toggle Recording

        D = Clear Positions

        e = TL Start
        E = TL Stop
        o = TL Steps
        O = TL Step

        r = Report All
        R = Report positions
        k = Keyframe report
        K = Request cam settings

        F = Set PT speed 1
        f = Set PT speed 2
        G = Set PT speed 3
        g = Set PT speed 4

        H = Set Sl speed 1
        h = Set Sl speed 2
        J = Set Sl speed 3
        j = Set Sl speed 4

        L = Set PT Accel
        l = Set Sl Accel

        t = Set Slide Limit
        T = Home Locate
        u = Set Home

        w = Set Zoom Limit

        s =
        W = 

        P = Toggle AutoFocus

        U = Store EEPROM
        */
        switch (instruction) {
          case 'Z':  // Save pos 1
            {
              if (camNumInst == '1') {
                Serial1.println("?M1");
              } else if (camNumInst == '2') {
                Serial2.println("?M1");
              } else if (camNumInst == '3') {
                Serial3.println("?M1");
              } else if (camNumInst == '4') {
                Serial4.println("?M1");
              } else if (camNumInst == '5') {
                Serial5.println("?M1");
              }
            }
            break;
          case 'X':  // Save pos 2
            {
              if (camNumInst == '1') {
                Serial1.println("?M2");
              } else if (camNumInst == '2') {
                Serial2.println("?M2");
              } else if (camNumInst == '3') {
                Serial3.println("?M2");
              } else if (camNumInst == '4') {
                Serial4.println("?M2");
              } else if (camNumInst == '5') {
                Serial5.println("?M2");
              }
            }
            break;
          case 'C':  // Save pos 3
            {
              if (camNumInst == '1') {
                Serial1.println("?M3");
              } else if (camNumInst == '2') {
                Serial2.println("?M3");
              } else if (camNumInst == '3') {
                Serial3.println("?M3");
              } else if (camNumInst == '4') {
                Serial4.println("?M3");
              } else if (camNumInst == '5') {
                Serial5.println("?M3");
              }
            }
            break;
          case 'V':  // Save pos 4
            {
              if (camNumInst == '1') {
                Serial1.println("?M4");
              } else if (camNumInst == '2') {
                Serial2.println("?M4");
              } else if (camNumInst == '3') {
                Serial3.println("?M4");
              } else if (camNumInst == '4') {
                Serial4.println("?M4");
              } else if (camNumInst == '5') {
                Serial5.println("?M4");
              }
            }
            break;
          case 'B':  // Save pos 5
            {
              if (camNumInst == '1') {
                Serial1.println("?M5");
              } else if (camNumInst == '2') {
                Serial2.println("?M5");
              } else if (camNumInst == '3') {
                Serial3.println("?M5");
              } else if (camNumInst == '4') {
                Serial4.println("?M5");
              } else if (camNumInst == '5') {
                Serial5.println("?M5");
              }
            }
            break;
          case 'N':  // Save pos 6
            {
              if (camNumInst == '1') {
                Serial1.println("?M6");
              } else if (camNumInst == '2') {
                Serial2.println("?M6");
              } else if (camNumInst == '3') {
                Serial3.println("?M6");
              } else if (camNumInst == '4') {
                Serial4.println("?M6");
              } else if (camNumInst == '5') {
                Serial5.println("?M6");
              }
            }
            break;
          case 'M':  // Save pos 7
            {
              if (camNumInst == '1') {
                Serial1.println("?M7");
              } else if (camNumInst == '2') {
                Serial2.println("?M7");
              } else if (camNumInst == '3') {
                Serial3.println("?M7");
              } else if (camNumInst == '4') {
                Serial4.println("?M7");
              } else if (camNumInst == '5') {
                Serial5.println("?M7");
              }
            }
            break;
          case '<':  // Save pos 8
            {
              if (camNumInst == '1') {
                Serial1.println("?M8");
              } else if (camNumInst == '2') {
                Serial2.println("?M8");
              } else if (camNumInst == '3') {
                Serial3.println("?M8");
              } else if (camNumInst == '4') {
                Serial4.println("?M8");
              } else if (camNumInst == '5') {
                Serial5.println("?M8");
              }
            }
            break;
          case '>':  // Save pos 9
            {
              if (camNumInst == '1') {
                Serial1.println("?M9");
              } else if (camNumInst == '2') {
                Serial2.println("?M9");
              } else if (camNumInst == '3') {
                Serial3.println("?M9");
              } else if (camNumInst == '4') {
                Serial4.println("?M9");
              } else if (camNumInst == '5') {
                Serial5.println("?M9");
              }
            }
            break;
          case '?':  // Save pos 10
            {
              if (camNumInst == '1') {
                Serial1.println("?M10");
              } else if (camNumInst == '2') {
                Serial2.println("?M10");
              } else if (camNumInst == '3') {
                Serial3.println("?M10");
              } else if (camNumInst == '4') {
                Serial4.println("?M10");
              } else if (camNumInst == '5') {
                Serial5.println("?M10");
              }
            }
            break;
          case 'z':  // Move to pos 1
            {
              if (camNumInst == '1') {
                Serial1.println("?m1");
              } else if (camNumInst == '2') {
                Serial2.println("?m1");
              } else if (camNumInst == '3') {
                Serial3.println("?m1");
              } else if (camNumInst == '4') {
                Serial4.println("?m1");
              } else if (camNumInst == '5') {
                Serial5.println("?m1");
              }
            }
            break;
          case 'x':  // Move to pos 2
            {
              if (camNumInst == '1') {
                Serial1.println("?m2");
              } else if (camNumInst == '2') {
                Serial2.println("?m2");
              } else if (camNumInst == '3') {
                Serial3.println("?m2");
              } else if (camNumInst == '4') {
                Serial4.println("?m2");
              } else if (camNumInst == '5') {
                Serial5.println("?m2");
              }
            }
            break;
          case 'c':  // Move to pos 3
            {
              if (camNumInst == '1') {
                Serial1.println("?m3");
              } else if (camNumInst == '2') {
                Serial2.println("?m3");
              } else if (camNumInst == '3') {
                Serial3.println("?m3");
              } else if (camNumInst == '4') {
                Serial4.println("?m3");
              } else if (camNumInst == '5') {
                Serial5.println("?m3");
              }
            }
            break;
          case 'v':  // Move to pos 4
            {
              if (camNumInst == '1') {
                Serial1.println("?m4");
              } else if (camNumInst == '2') {
                Serial2.println("?m4");
              } else if (camNumInst == '3') {
                Serial3.println("?m4");
              } else if (camNumInst == '4') {
                Serial4.println("?m4");
              } else if (camNumInst == '5') {
                Serial5.println("?m4");
              }
            }
            break;
          case 'b':  // Move to pos 5
            {
              if (camNumInst == '1') {
                Serial1.println("?m5");
              } else if (camNumInst == '2') {
                Serial2.println("?m5");
              } else if (camNumInst == '3') {
                Serial3.println("?m5");
              } else if (camNumInst == '4') {
                Serial4.println("?m5");
              } else if (camNumInst == '5') {
                Serial5.println("?m5");
              }
            }
            break;
          case 'n':  // Move to pos 6
            {
              if (camNumInst == '1') {
                Serial1.println("?m6");
              } else if (camNumInst == '2') {
                Serial2.println("?m6");
              } else if (camNumInst == '3') {
                Serial3.println("?m6");
              } else if (camNumInst == '4') {
                Serial4.println("?m6");
              } else if (camNumInst == '5') {
                Serial5.println("?m6");
              }
            }
            break;
          case 'm':  // Move to pos 7
            {
              if (camNumInst == '1') {
                Serial1.println("?m7");
              } else if (camNumInst == '2') {
                Serial2.println("?m7");
              } else if (camNumInst == '3') {
                Serial3.println("?m7");
              } else if (camNumInst == '4') {
                Serial4.println("?m7");
              } else if (camNumInst == '5') {
                Serial5.println("?m7");
              }
            }
            break;
          case ',':  // Move to pos 8
            {
              if (camNumInst == '1') {
                Serial1.println("?m8");
              } else if (camNumInst == '2') {
                Serial2.println("?m8");
              } else if (camNumInst == '3') {
                Serial3.println("?m8");
              } else if (camNumInst == '4') {
                Serial4.println("?m8");
              } else if (camNumInst == '5') {
                Serial5.println("?m8");
              }
            }
            break;
          case '.':  // Move to pos 9
            {
              if (camNumInst == '1') {
                Serial1.println("?m9");
              } else if (camNumInst == '2') {
                Serial2.println("?m9");
              } else if (camNumInst == '3') {
                Serial3.println("?m9");
              } else if (camNumInst == '4') {
                Serial4.println("?m9");
              } else if (camNumInst == '5') {
                Serial5.println("?m9");
              }
            }
            break;
          case '/':  // Move to pos 10
            {
              if (camNumInst == '1') {
                Serial1.println("?m10");
              } else if (camNumInst == '2') {
                Serial2.println("?m10");
              } else if (camNumInst == '3') {
                Serial3.println("?m10");
              } else if (camNumInst == '4') {
                Serial4.println("?m10");
              } else if (camNumInst == '5') {
                Serial5.println("?m10");
              }
            }
            break;
          case 'y':  // Move to pos 1    - Slider Only
            {
              if (camNumInst == '4') {
                Serial4.println("?v");
              } else if (camNumInst == '5') {
                Serial5.println("?v");
              }
            }
            break;
          case 'Y':  // Move to pos 10   - Slider Only
            {
              if (camNumInst == '4') {
                Serial4.println("?V");
              } else if (camNumInst == '5') {
                Serial5.println("?V");
              }
            }
            break;
          case 'a':  // Zoom out
            {
              instruction = Serial.read();
              if (instruction == '8') {
                if (camNumInst == '1') {
                  Serial1.println("?z8");
                } else if (camNumInst == '2') {
                  Serial2.println("?z8");
                } else if (camNumInst == '3') {
                  Serial3.println("?z8");
                } else if (camNumInst == '4') {
                  Serial4.println("?z8");
                } else if (camNumInst == '5') {
                  Serial5.println("?z8");
                }
              } else if (instruction == '7') {
                if (camNumInst == '1') {
                  Serial1.println("?z7");
                } else if (camNumInst == '2') {
                  Serial2.println("?z7");
                } else if (camNumInst == '3') {
                  Serial3.println("?z7");
                } else if (camNumInst == '4') {
                  Serial4.println("?z7");
                } else if (camNumInst == '5') {
                  Serial5.println("?z7");
                }
              } else if (instruction == '6') {
                if (camNumInst == '1') {
                  Serial1.println("?z6");
                } else if (camNumInst == '2') {
                  Serial2.println("?z6");
                } else if (camNumInst == '3') {
                  Serial3.println("?z6");
                } else if (camNumInst == '4') {
                  Serial4.println("?z6");
                } else if (camNumInst == '5') {
                  Serial5.println("?z6");
                }
              } else if (instruction == '5') {
                if (camNumInst == '1') {
                  Serial1.println("?z5");
                } else if (camNumInst == '2') {
                  Serial2.println("?z5");
                } else if (camNumInst == '3') {
                  Serial3.println("?z5");
                } else if (camNumInst == '4') {
                  Serial4.println("?z5");
                } else if (camNumInst == '5') {
                  Serial5.println("?z5");
                }
              } else if (instruction == '4') {
                if (camNumInst == '1') {
                  Serial1.println("?z4");
                } else if (camNumInst == '2') {
                  Serial2.println("?z4");
                } else if (camNumInst == '3') {
                  Serial3.println("?z4");
                } else if (camNumInst == '4') {
                  Serial4.println("?z4");
                } else if (camNumInst == '5') {
                  Serial5.println("?z4");
                }
              } else if (instruction == '3') {
                if (camNumInst == '1') {
                  Serial1.println("?z3");
                } else if (camNumInst == '2') {
                  Serial2.println("?z3");
                } else if (camNumInst == '3') {
                  Serial3.println("?z3");
                } else if (camNumInst == '4') {
                  Serial4.println("?z3");
                } else if (camNumInst == '5') {
                  Serial5.println("?z3");
                }
              } else if (instruction == '2') {
                if (camNumInst == '1') {
                  Serial1.println("?z2");
                } else if (camNumInst == '2') {
                  Serial2.println("?z2");
                } else if (camNumInst == '3') {
                  Serial3.println("?z2");
                } else if (camNumInst == '4') {
                  Serial4.println("?z2");
                } else if (camNumInst == '5') {
                  Serial5.println("?z2");
                }
              } else if (instruction == '1') {
                if (camNumInst == '1') {
                  Serial1.println("?z1");
                } else if (camNumInst == '2') {
                  Serial2.println("?z1");
                } else if (camNumInst == '3') {
                  Serial3.println("?z1");
                } else if (camNumInst == '4') {
                  Serial4.println("?z1");
                } else if (camNumInst == '5') {
                  Serial5.println("?z1");
                }
              }
            }
            break;
          case 'A':  // Zoom in
            {
              instruction = Serial.read();
              if (instruction == '8') {
                if (camNumInst == '1') {
                  Serial1.println("?Z8");
                } else if (camNumInst == '2') {
                  Serial2.println("?Z8");
                } else if (camNumInst == '3') {
                  Serial3.println("?Z8");
                } else if (camNumInst == '4') {
                  Serial4.println("?Z8");
                } else if (camNumInst == '5') {
                  Serial5.println("?Z8");
                }
              } else if (instruction == '7') {
                if (camNumInst == '1') {
                  Serial1.println("?Z7");
                } else if (camNumInst == '2') {
                  Serial2.println("?Z7");
                } else if (camNumInst == '3') {
                  Serial3.println("?Z7");
                } else if (camNumInst == '4') {
                  Serial4.println("?Z7");
                } else if (camNumInst == '5') {
                  Serial5.println("?Z7");
                }
              } else if (instruction == '6') {
                if (camNumInst == '1') {
                  Serial1.println("?Z6");
                } else if (camNumInst == '2') {
                  Serial2.println("?Z6");
                } else if (camNumInst == '3') {
                  Serial3.println("?Z6");
                } else if (camNumInst == '4') {
                  Serial4.println("?Z6");
                } else if (camNumInst == '5') {
                  Serial5.println("?Z6");
                }
              } else if (instruction == '5') {
                if (camNumInst == '1') {
                  Serial1.println("?Z5");
                } else if (camNumInst == '2') {
                  Serial2.println("?Z5");
                } else if (camNumInst == '3') {
                  Serial3.println("?Z5");
                } else if (camNumInst == '4') {
                  Serial4.println("?Z5");
                } else if (camNumInst == '5') {
                  Serial5.println("?Z5");
                }
              } else if (instruction == '4') {
                if (camNumInst == '1') {
                  Serial1.println("?Z4");
                } else if (camNumInst == '2') {
                  Serial2.println("?Z4");
                } else if (camNumInst == '3') {
                  Serial3.println("?Z4");
                } else if (camNumInst == '4') {
                  Serial4.println("?Z4");
                } else if (camNumInst == '5') {
                  Serial5.println("?Z4");
                }
              } else if (instruction == '3') {
                if (camNumInst == '1') {
                  Serial1.println("?Z3");
                } else if (camNumInst == '2') {
                  Serial2.println("?Z3");
                } else if (camNumInst == '3') {
                  Serial3.println("?Z3");
                } else if (camNumInst == '4') {
                  Serial4.println("?Z3");
                } else if (camNumInst == '5') {
                  Serial5.println("?Z3");
                }
              } else if (instruction == '2') {
                if (camNumInst == '1') {
                  Serial1.println("?Z2");
                } else if (camNumInst == '2') {
                  Serial2.println("?Z2");
                } else if (camNumInst == '3') {
                  Serial3.println("?Z2");
                } else if (camNumInst == '4') {
                  Serial4.println("?Z2");
                } else if (camNumInst == '5') {
                  Serial5.println("?Z2");
                }
              } else if (instruction == '1') {
                if (camNumInst == '1') {
                  Serial1.println("?Z1");
                } else if (camNumInst == '2') {
                  Serial2.println("?Z1");
                } else if (camNumInst == '3') {
                  Serial3.println("?Z1");
                } else if (camNumInst == '4') {
                  Serial4.println("?Z1");
                } else if (camNumInst == '5') {
                  Serial5.println("?Z1");
                }
              }
            }
            break;
          case 'q':  // Zoom STOP
            {
              if (camNumInst == '1') {
                Serial1.println("?N");
              } else if (camNumInst == '2') {
                Serial2.println("?N");
              } else if (camNumInst == '3') {
                Serial3.println("?N");
              } else if (camNumInst == '4') {
                Serial4.println("?N");
              } else if (camNumInst == '5') {
                Serial5.println("?N");
              }
            }
            break;
          case 'd':
            {  // Toggle recording
              if (camNumInst == '1') {
                Serial1.println("?u");
              } else if (camNumInst == '2') {
                Serial2.println("?u");
              } else if (camNumInst == '3') {
                Serial3.println("?u");
              } else if (camNumInst == '4') {
                Serial4.println("?u");
              } else if (camNumInst == '5') {
                Serial5.println("?u");
              }
            }
            break;
          case 'D':
            {  // Clear Positions
              if (camNumInst == '1') {
                Serial1.println("?Y");
              } else if (camNumInst == '2') {
                Serial2.println("?Y");
              } else if (camNumInst == '3') {
                Serial3.println("?Y");
              } else if (camNumInst == '4') {
                Serial4.println("?Y");
              } else if (camNumInst == '5') {
                Serial5.println("?Y");
              }
            }
            break;
          case 'e':
            {  // TL Start
              if (camNumInst == '1') {
                Serial1.println("?K");
              } else if (camNumInst == '2') {
                Serial2.println("?K");
              } else if (camNumInst == '3') {
                Serial3.println("?K");
              } else if (camNumInst == '4') {
                Serial4.println("?K");
              } else if (camNumInst == '5') {
                Serial5.println("?K");
              }
            }
            break;
          case 'E':
            {  // TL Stop
              if (camNumInst == '1') {
                Serial1.println("?n");
              } else if (camNumInst == '2') {
                Serial2.println("?n");
              } else if (camNumInst == '3') {
                Serial3.println("?n");
              } else if (camNumInst == '4') {
                Serial4.println("?n");
              } else if (camNumInst == '5') {
                Serial5.println("?n");
              }
            }
            break;
          case 'o':
            {  // TL Steps
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?L") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial1.println(String("?L") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial1.println(String("?L") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial1.println(String("?L") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial1.println(String("?L") + SerialCommandValueInt);
              }
            }
            break;
          case 'O':
            {  // TL Step
              if (camNumInst == '1') {
                Serial1.println("?A");
              } else if (camNumInst == '2') {
                Serial2.println("?A");
              } else if (camNumInst == '3') {
                Serial3.println("?A");
              } else if (camNumInst == '4') {
                Serial4.println("?A");
              } else if (camNumInst == '5') {
                Serial5.println("?A");
              }
            }
            break;
          case 'i':
            {  // Toggle use Set Speeds
              if (camNumInst == '1') {
                Serial1.println("?i");
              } else if (camNumInst == '2') {
                Serial2.println("?i");
              } else if (camNumInst == '3') {
                Serial3.println("?i");
              } else if (camNumInst == '4') {
                Serial4.println("?i");
              } else if (camNumInst == '5') {
                Serial5.println("?i");
              }
            }
            break;
          case 'r':
            {  // Report
              if (camNumInst == '1') {
                Serial1.println("?r");
              } else if (camNumInst == '2') {
                Serial2.println("?r");
              } else if (camNumInst == '3') {
                Serial3.println("?r");
              } else if (camNumInst == '4') {
                Serial4.println("?r");
              } else if (camNumInst == '5') {
                Serial5.println("?r");
              }
            }
            break;
          case 'R':
            {  // Position Report
              if (camNumInst == '1') {
                Serial1.println("?R");
              } else if (camNumInst == '2') {
                Serial2.println("?R");
              } else if (camNumInst == '3') {
                Serial3.println("?R");
              } else if (camNumInst == '4') {
                Serial4.println("?R");
              } else if (camNumInst == '5') {
                Serial5.println("?R");
              }
            }
            break;
          case 'k':
            {  // Keyframe Report
              if (camNumInst == '1') {
                Serial1.println("?k");
              } else if (camNumInst == '2') {
                Serial2.println("?k");
              } else if (camNumInst == '3') {
                Serial3.println("?k");
              } else if (camNumInst == '4') {
                Serial4.println("?k");
              } else if (camNumInst == '5') {
                Serial5.println("?k");
              }
            }
            break;
          case 'K':
            {  // Request cam settings
              if (camNumInst == '1') {
                Serial1.println("?F");
              } else if (camNumInst == '2') {
                Serial2.println("?F");
              } else if (camNumInst == '3') {
                Serial3.println("?F");
              } else if (camNumInst == '4') {
                Serial4.println("?F");
              } else if (camNumInst == '5') {
                Serial5.println("?F");
              }
            }
            break;
          case 'F':
            {  // set PT speed 1
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?B") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?B") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?B") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?B") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?B") + SerialCommandValueInt);
              }
            }
            break;
          case 'f':
            {  // set PT speed 2
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?b") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?b") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?b") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?b") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?b") + SerialCommandValueInt);
              }
            }
            break;
          case 'G':
            {  // set PT speed 3
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?C") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?C") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?C") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?C") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?C") + SerialCommandValueInt);
              }
            }
            break;
          case 'g':
            {  // set PT speed 4
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?c") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?c") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?c") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?c") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?c") + SerialCommandValueInt);
              }
            }
            break;
          case 'H':
            {  // set SL speed 1
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?D") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?D") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?D") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?D") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?D") + SerialCommandValueInt);
              }
            }
            break;
          case 'h':
            {  // set SL speed 2
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?d") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?d") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?d") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?d") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?d") + SerialCommandValueInt);
              }
            }
            break;
          case 'J':
            {  // set SL speed 3
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?E") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?E") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?E") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?E") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?E") + SerialCommandValueInt);
              }
            }
            break;
          case 'j':
            {  // set SL speed 4
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?e") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?e") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?e") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?e") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?e") + SerialCommandValueInt);
              }
            }
            break;
          case 'L':
            {  // set PT Accel
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?Q") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?Q") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?Q") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?Q") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?Q") + SerialCommandValueInt);
              }
            }
            break;
          case 'l':
            {  // set SL Accel
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?q") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?q") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?q") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?q") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?q") + SerialCommandValueInt);
              }
            }
            break;
          case 't':
            {  // set Slide Limit
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?y") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?y") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?y") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?y") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?y") + SerialCommandValueInt);
              }
            }
            break;
          case 'T':
            {  // Locate home (for limits)
              String stringText = Serial.readStringUntil('\n');
              if (camNumInst == '1') {
                Serial1.println(String("?H"));
              } else if (camNumInst == '2') {
                Serial2.println(String("?H"));
              } else if (camNumInst == '3') {
                Serial3.println(String("?H"));
              } else if (camNumInst == '4') {
                Serial4.println(String("?H"));
              } else if (camNumInst == '5') {
                Serial5.println(String("?H"));
              }
            }
            break;
          case 'u':
            {  // Set home (for limits)
              String stringText = Serial.readStringUntil('\n');
              if (camNumInst == '1') {
                Serial1.println(String("?h"));
              } else if (camNumInst == '2') {
                Serial2.println(String("?h"));
              } else if (camNumInst == '3') {
                Serial3.println(String("?h"));
              } else if (camNumInst == '4') {
                Serial4.println(String("?h"));
              } else if (camNumInst == '5') {
                Serial5.println(String("?h"));
              }
            }
            break;
          case 'w':
            {  // set Zoom Limit
              String stringText = Serial.readStringUntil('\n');
              SerialCommandValueInt = stringText.toInt();
              if (camNumInst == '1') {
                Serial1.println(String("?w") + SerialCommandValueInt);
              } else if (camNumInst == '2') {
                Serial2.println(String("?w") + SerialCommandValueInt);
              } else if (camNumInst == '3') {
                Serial3.println(String("?w") + SerialCommandValueInt);
              } else if (camNumInst == '4') {
                Serial4.println(String("?w") + SerialCommandValueInt);
              } else if (camNumInst == '5') {
                Serial5.println(String("?w") + SerialCommandValueInt);
              }
            }
            break;
          case 's':
            {
              while (Serial.available() < 1) {  //  Wait for 1 byte to be available.
                //delayMicroseconds(1);
                ;
              }
              instruction = Serial.read();
              if (instruction == '1') {
                if (camNumInst == '1') {
                  Serial1.println("?s1");
                } else if (camNumInst == '2') {
                  Serial2.println("?s1");
                } else if (camNumInst == '3') {
                  Serial3.println("?s1");
                } else if (camNumInst == '4') {
                  Serial4.println("?s1");
                } else if (camNumInst == '5') {
                  Serial5.println("?s1");
                }
              } else if (instruction == '2') {
                if (camNumInst == '1') {
                  Serial1.println("?s2");
                } else if (camNumInst == '2') {
                  Serial2.println("?s2");
                } else if (camNumInst == '3') {
                  Serial3.println("?s2");
                } else if (camNumInst == '4') {
                  Serial4.println("?s2");
                } else if (camNumInst == '5') {
                  Serial5.println("?s2");
                }
              } else if (instruction == '3') {
                if (camNumInst == '1') {
                  Serial1.println("?s3");
                } else if (camNumInst == '2') {
                  Serial2.println("?s3");
                } else if (camNumInst == '3') {
                  Serial3.println("?s3");
                } else if (camNumInst == '4') {
                  Serial4.println("?s3");
                } else if (camNumInst == '5') {
                  Serial5.println("?s3");
                }
              } else if (instruction == '4') {
                if (camNumInst == '1') {
                  Serial1.println("?s4");
                } else if (camNumInst == '2') {
                  Serial2.println("?s4");
                } else if (camNumInst == '3') {
                  Serial3.println("?s4");
                } else if (camNumInst == '4') {
                  Serial4.println("?s4");
                } else if (camNumInst == '5') {
                  Serial5.println("?s4");
                }
              }
            }
            break;
          case 'W':
            {
              while (Serial.available() < 1) {  //  Wait for 1 byte to be available.
                //delayMicroseconds(1);
                ;
              }
              instruction = Serial.read();
              if (instruction == '1') {
                if (camNumInst == '1') {
                  Serial1.println("?a1");
                } else if (camNumInst == '2') {
                  Serial2.println("?a1");
                } else if (camNumInst == '3') {
                  Serial3.println("?a1");
                } else if (camNumInst == '4') {
                  Serial4.println("?a1");
                } else if (camNumInst == '5') {
                  Serial5.println("?a1");
                }
              } else if (instruction == '2') {
                if (camNumInst == '1') {
                  Serial1.println("?a2");
                } else if (camNumInst == '2') {
                  Serial2.println("?a2");
                } else if (camNumInst == '3') {
                  Serial3.println("?a2");
                } else if (camNumInst == '4') {
                  Serial4.println("?a2");
                } else if (camNumInst == '5') {
                  Serial5.println("?a2");
                }
              } else if (instruction == '3') {
                if (camNumInst == '1') {
                  Serial1.println("?a3");
                } else if (camNumInst == '2') {
                  Serial2.println("?a3");
                } else if (camNumInst == '3') {
                  Serial3.println("?a3");
                } else if (camNumInst == '4') {
                  Serial4.println("?a3");
                } else if (camNumInst == '5') {
                  Serial5.println("?a3");
                }
              } else if (instruction == '4') {
                if (camNumInst == '1') {
                  Serial1.println("?a4");
                } else if (camNumInst == '2') {
                  Serial2.println("?a4");
                } else if (camNumInst == '3') {
                  Serial3.println("?a4");
                } else if (camNumInst == '4') {
                  Serial4.println("?a4");
                } else if (camNumInst == '5') {
                  Serial5.println("?a4");
                }
              }
            }
            break;
          case '$':  // is string command
            {
              while (Serial.available() > 0) {
                char received = Serial.read();
                inData6 = "?";
                inData6 += received;
                if (received == '\n') {
                  if (camNumInst == '1') {
                    Serial1.println(inData6);
                  } else if (camNumInst == '2') {
                    Serial2.println(inData6);
                  } else if (camNumInst == '3') {
                    Serial3.println(inData6);
                  } else if (camNumInst == '4') {
                    Serial4.println(inData6);
                  } else if (camNumInst == '5') {
                    Serial5.println(inData6);
                  }

                  Serial2.println(inData6);
                  inData6 = "";
                }
              }
            }
            break;
          case 'U':
            {  // Store EEPROM
              if (camNumInst == '1') {
                Serial1.println("?U");
              } else if (camNumInst == '2') {
                Serial2.println("?U");
              } else if (camNumInst == '3') {
                Serial3.println("?U");
              } else if (camNumInst == '4') {
                Serial4.println("?U");
              } else if (camNumInst == '5') {
                Serial5.println("?U");
              }
            }
            break;
        }
      } else if (instruction == '-') {
        resetLEDs = true;
      } else if (instruction == '?') {
        while (Serial.available() > 0) {
          char received = Serial.read();
          inData6 += received;
          if (received == '\n') {
            Serial1.println(inData6);
            inData6 = "";
          }
        }
      } else if (instruction == '!') {
        while (Serial.available() > 0) {
          char received = Serial.read();
          inData6 += received;
          if (received == '\n') {
            Serial2.println(inData6);
            inData6 = "";
          }
        }
      } else if (instruction == '@') {
        while (Serial.available() > 0) {
          char received = Serial.read();
          inData6 += received;
          if (received == '\n') {
            Serial3.println(inData6);
            inData6 = "";
          }
        }
      } else if (instruction == '&') {
        while (Serial.available() > 0) {
          char received = Serial.read();
          inData6 += received;
          if (received == '\n') {
            Serial4.println(inData6);
            inData6 = "";
          }
        }
      } else if (instruction == '*') {
        while (Serial.available() > 0) {
          char received = Serial.read();
          inData6 += received;
          if (received == '\n') {
            Serial5.println(inData6);
            inData6 = "";
          }
        }
      }
    }
  }
}