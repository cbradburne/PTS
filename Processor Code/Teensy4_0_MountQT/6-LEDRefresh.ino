//  Send LED status to remote

void doRemoteControlLEDs(void) {
  if (atPos1) {
    Serial1.println("#z");
    Serial1.println("#z");
  }
  if (pos1set) {
    Serial1.println("#Z");
    Serial1.println("#Z");
  }
  if (atPos2) {
    Serial1.println("#x");
    Serial1.println("#x");
  }
  if (pos2set) {
    Serial1.println("#X");
    Serial1.println("#X");
  }
  if (atPos3) {
    Serial1.println("#c");
    Serial1.println("#c");
  }
  if (pos3set) {
    Serial1.println("#C");
    Serial1.println("#C");
  }
  if (atPos4) {
    Serial1.println("#v");
    Serial1.println("#v");
  }
  if (pos4set) {
    Serial1.println("#V");
    Serial1.println("#V");
  }
  if (atPos5) {
    Serial1.println("#b");
    Serial1.println("#b");
  }
  if (pos5set) {
    Serial1.println("#B");
    Serial1.println("#B");
  }
  if (atPos6) {
    Serial1.println("#n");
    Serial1.println("#n");
  }
  if (pos6set) {
    Serial1.println("#N");
    Serial1.println("#N");
  }
  if (atPos7) {
    Serial1.println("#m");
    Serial1.println("#m");
  }
  if (pos7set) {
    Serial1.println("#M");
    Serial1.println("#M");
  }
  if (atPos8) {
    Serial1.println("#,");
    Serial1.println("#,");
  }
  if (pos8set) {
    Serial1.println("#<");
    Serial1.println("#<");
  }
  if (atPos9) {
    Serial1.println("#.");
    Serial1.println("#.");
  }
  if (pos9set) {
    Serial1.println("#>");
    Serial1.println("#>");
  }
  if (atPos0) {
    Serial1.println("#/");
    Serial1.println("#/");
  }
  if (pos0set) {
    Serial1.println("#?");
    Serial1.println("#?");
  }

  Serial1.println("#+");

  if (pantilt_set_speed == pantilt_speed1) {
    Serial1.println("^@1");
    Serial1.println("^@1");
  } else if (pantilt_set_speed == pantilt_speed2) {
    Serial1.println("^@3");
    Serial1.println("^@3");
  } else if (pantilt_set_speed == pantilt_speed3) {
    Serial1.println("^@5");
    Serial1.println("^@5");
  } else if (pantilt_set_speed == pantilt_speed4) {
    Serial1.println("^@7");
    Serial1.println("^@7");
  }

  if (withSlider) {
    if (slider_set_speed == slider_speed1) {
      Serial1.println("^=1");
      Serial1.println("^=1");
    } else if (slider_set_speed == slider_speed2) {
      Serial1.println("^=3");
      Serial1.println("^=3");
    } else if (slider_set_speed == slider_speed3) {
      Serial1.println("^=5");
      Serial1.println("^=5");
    } else if (slider_set_speed == slider_speed4) {
      Serial1.println("^=7");
      Serial1.println("^=7");
    }
  }
}