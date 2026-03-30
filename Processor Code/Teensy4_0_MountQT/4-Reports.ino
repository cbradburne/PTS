//  Reports

void debugReport(void) {
  Serial1.println("Debug Report:");
  Serial1.println(String("Pan angle           : ") + panStepsToDegrees(stepper_pan.getPosition()) + String(" Deg"));
  Serial1.println(String("Tilt angle          : ") + tiltStepsToDegrees(stepper_tilt.getPosition()) + String(" Deg"));
  Serial1.println(String("Slider position     : ") + sliderStepsToMillimetres(stepper_slider.getPosition()) + String(" mm\n"));
  Serial1.println(String("Zoom position       : ") + stepper_zoom.getPosition());

  Serial1.println(String("Pan/TILT steps      : ") + panDegreesToSteps(pantilt_set_speed) + String(" steps/s"));
  Serial1.println(String("Slider steps        : ") + sliderMillimetresToSteps(slider_set_speed) + String(" steps/s\n"));
  
  Serial1.println(String("Pan/Tilt set speed  : ") + pantilt_set_speed + String(" Deg/s"));
  Serial1.println(String("Slider set speed    : ") + slider_set_speed + String(" mm/s\n"));
  Serial1.println(String("Pan/Tilt Base Accel : ") + pantilt_accel + String(" steps/sSq"));
  Serial1.println(String("Slider Base Accel   : ") + slider_accel + String(" steps/sSq\n"));
  Serial1.println(String("Slider Limit        : ") + slideLimit + String(" mm\n"));
  Serial1.println(String("Zoom Limit          : ") + zoomLimit + String("\n"));
  
  Serial1.println(String("Pan/Tilt Speed 1    : ") + pantilt_speed1 + String(" Deg/s"));
  Serial1.println(String("Pan/Tilt Speed 2    : ") + pantilt_speed2 + String(" Deg/s"));
  Serial1.println(String("Pan/Tilt Speed 3    : ") + pantilt_speed3 + String(" Deg/s"));
  Serial1.println(String("Pan/Tilt Speed 4    : ") + pantilt_speed4 + String(" Deg/s"));
  Serial1.println(String("Slider Speed 1      : ") + slider_speed1 + String(" mm/s"));
  Serial1.println(String("Slider Speed 2      : ") + slider_speed2 + String(" mm/s"));
  Serial1.println(String("Slider Speed 3      : ") + slider_speed3 + String(" mm/s"));
  Serial1.println(String("Slider Speed 4      : ") + slider_speed4 + String(" mm/s"));

  printEEPROM();
}


/*--------------------------------------------------------------------------------------------------------------------------------------------------------*/


void positionReport(void) {
  Serial1.println("Position Report:");
  Serial1.println(String("Pan angle         : ") + panStepsToDegrees(stepper_pan.getPosition()) + String(" Deg"));
  Serial1.println(String("Tilt angle        : ") + tiltStepsToDegrees(stepper_tilt.getPosition()) + String(" Deg"));
  Serial1.println(String("Slider position   : ") + sliderStepsToMillimetres(stepper_slider.getPosition()) + String(" mm\n"));
  Serial1.println(String("Zoom position     : ") + stepper_zoom.getPosition());
  Serial1.println("#$");
}


/*--------------------------------------------------------------------------------------------------------------------------------------------------------*/


void printKeyframeElements(void) {
  int row = 0;
  do {
    Serial1.print(String("Keyframe index: ") + (row + 1) + "\n");
    Serial1.print(String("Pan   : ") + panStepsToDegrees(keyframe_array[row].panStepCount) + String(" Deg\t"));
    Serial1.print(String("Tilt  : ") + tiltStepsToDegrees(keyframe_array[row].tiltStepCount) + String(" Deg\t"));
    Serial1.print(String("Slider: ") + sliderStepsToMillimetres(keyframe_array[row].sliderStepCount) + String(" mm\t"));
    Serial1.print(String("Zoom  : ") + keyframe_array[row].zoomStepCount);
    Serial1.print(String("Pan/Tilt Speed : ") + panStepsToDegrees(keyframe_array[row].panTiltSpeed) + String(" Deg/s\t"));
    Serial1.print(String("Slider Speed : ") + sliderStepsToMillimetres(keyframe_array[row].sliderSpeed) + String(" mm/s\n"));

    row++;
  } while (row < 10);

  Serial1.print("\n");
  Serial1.println("#$");
}