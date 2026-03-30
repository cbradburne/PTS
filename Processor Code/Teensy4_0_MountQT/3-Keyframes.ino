//  Keyframes - Moveto, Edit & Clear

void editKeyframe(int keyframeEdit) {
  keyframe_array[(keyframeEdit - 1)].panStepCount = stepper_pan.getPosition();
  keyframe_array[(keyframeEdit - 1)].tiltStepCount = stepper_tilt.getPosition();
  keyframe_array[(keyframeEdit - 1)].panTiltSpeed = panDegreesToSteps(pantilt_set_speed);
  keyframe_array[(keyframeEdit - 1)].sliderStepCount = stepper_slider.getPosition();
  keyframe_array[(keyframeEdit - 1)].sliderSpeed = sliderMillimetresToSteps(slider_set_speed);
  keyframe_array[(keyframeEdit - 1)].zoomStepCount = stepper_zoom.getPosition();
  keyframe_array[(keyframeEdit - 1)].isRecorded = 1;

  if (keyframeEdit == 1) {
    pos1set = true;
    atPos1 = true;
    Serial1.println("#z");
    Serial1.println("#z");
    Serial1.println("#Z");
    Serial1.println("#Z");
  } else if (keyframeEdit == 2) {
    pos2set = true;
    atPos2 = true;
    Serial1.println("#x");
    Serial1.println("#x");
    Serial1.println("#X");
    Serial1.println("#X");
  } else if (keyframeEdit == 3) {
    pos3set = true;
    atPos3 = true;
    Serial1.println("#c");
    Serial1.println("#c");
    Serial1.println("#C");
    Serial1.println("#C");
  } else if (keyframeEdit == 4) {
    pos4set = true;
    atPos4 = true;
    Serial1.println("#v");
    Serial1.println("#v");
    Serial1.println("#V");
    Serial1.println("#V");
  } else if (keyframeEdit == 5) {
    pos5set = true;
    atPos5 = true;
    Serial1.println("#b");
    Serial1.println("#b");
    Serial1.println("#B");
    Serial1.println("#B");
  } else if (keyframeEdit == 6) {
    pos6set = true;
    atPos6 = true;
    Serial1.println("#n");
    Serial1.println("#n");
    Serial1.println("#N");
    Serial1.println("#N");
  } else if (keyframeEdit == 7) {
    pos7set = true;
    atPos7 = true;
    Serial1.println("#m");
    Serial1.println("#m");
    Serial1.println("#M");
    Serial1.println("#M");
  } else if (keyframeEdit == 8) {
    pos8set = true;
    atPos8 = true;
    Serial1.println("#,");
    Serial1.println("#,");
    Serial1.println("#<");
    Serial1.println("#<");
  } else if (keyframeEdit == 9) {
    pos9set = true;
    atPos9 = true;
    Serial1.println("#.");
    Serial1.println("#.");
    Serial1.println("#>");
    Serial1.println("#>");
  } else if (keyframeEdit == 10) {
    pos0set = true;
    atPos0 = true;
    Serial1.println("#/");
    Serial1.println("#/");
    Serial1.println("#?");
    Serial1.println("#?");
  }
  Serial1.println(String("Edited index: ") + keyframeEdit);
  Serial1.println(String("Pan angle         : ") + panStepsToDegrees(stepper_pan.getPosition()) + String("°"));
  Serial1.println(String("Tilt angle        : ") + tiltStepsToDegrees(stepper_tilt.getPosition()) + String("°"));
  Serial1.println(String("Slider position   : ") + sliderStepsToMillimetres(stepper_slider.getPosition()) + String("mm"));
  Serial1.println(String("Zoom position     : ") + stepper_zoom.getPosition());
  Serial1.println("#$");

  sentMoved = false;
}


/*--------------------------------------------------------------------------------------------------------------------------------------------------------*/


void clearKeyframes(void) {

  for (int i = 0; i < 10; i++) {
    keyframe_array[i].panStepCount = 0;
    keyframe_array[i].tiltStepCount = 0;
    keyframe_array[i].panTiltSpeed = 0;
    keyframe_array[i].sliderStepCount = 0;
    keyframe_array[i].sliderSpeed = 0;
    keyframe_array[i].zoomStepCount = 0;
    keyframe_array[i].isRecorded = 0;
  }

  pos1set = false;
  pos2set = false;
  pos3set = false;
  pos4set = false;
  pos5set = false;
  pos6set = false;
  pos7set = false;
  pos8set = false;
  pos9set = false;
  pos0set = false;

  atPos1 = false;
  atPos2 = false;
  atPos3 = false;
  atPos4 = false;
  atPos5 = false;
  atPos6 = false;
  atPos7 = false;
  atPos8 = false;
  atPos9 = false;
  atPos0 = false;

  Serial1.println("#a");
  Serial1.println("#a");
  Serial1.println("Positions Cleared.\n");
  Serial1.println("#$");
}


/*--------------------------------------------------------------------------------------------------------------------------------------------------------*/


void moveSliderToEnd1(){
  //Serial1.println("#A"); 
  //Serial1.println("#A"); 
  stepper_slider.moveAbsAsync(keyframe_array[0].sliderStepCount);
}

void moveSliderToEnd2(){
  //Serial1.println("#:");
  //Serial1.println("#:");
  stepper_slider.moveAbsAsync(keyframe_array[9].sliderStepCount);
}


void moveToIndex(int index) {
  if (keyframe_array[index - 1].isRecorded == 0) {
    return;
  }

  if (index == 1) { 
    Serial1.println("#A"); 
    Serial1.println("#A"); 
    Serial2.println("#A"); 
    } else if (index == 2) { 
    Serial1.println("#S"); 
    Serial1.println("#S"); 
    Serial2.println("#S"); 
    } else if (index == 3) { 
    Serial1.println("#D"); 
    Serial1.println("#D"); 
    Serial2.println("#D"); 
    } else if (index == 4) { 
    Serial1.println("#F"); 
    Serial1.println("#F"); 
    Serial2.println("#F"); 
    } else if (index == 5) { 
    Serial1.println("#G"); 
    Serial1.println("#G"); 
    Serial2.println("#G"); 
    } else if (index == 6) { 
    Serial1.println("#H"); 
    Serial1.println("#H"); 
    Serial2.println("#H"); 
    } else if (index == 7) { 
    Serial1.println("#J"); 
    Serial1.println("#J"); 
    Serial2.println("#J"); 
    } else if (index == 8) { 
    Serial1.println("#K"); 
    Serial1.println("#K"); 
    Serial2.println("#K"); 
    } else if (index == 9) { 
    Serial1.println("#L"); 
    Serial1.println("#L"); 
    Serial2.println("#L"); 
    } else if (index == 10) { 
    Serial1.println("#:"); 
    Serial1.println("#:"); 
    Serial2.println("#:"); 
    }

  Serial1.println(String("Moving to Index: ") + index);
  Serial1.println(String("Pan   : ") + panStepsToDegrees(keyframe_array[index - 1].panStepCount) + String("°"));
  Serial1.println(String("Tilt  : ") + tiltStepsToDegrees(keyframe_array[index - 1].tiltStepCount) + String("°"));
  Serial1.println(String("Slider: ") + sliderStepsToMillimetres(keyframe_array[index - 1].sliderStepCount) + String("mm"));
  Serial1.println(String("Zoom  : ") + (keyframe_array[index - 1].zoomStepCount));
  Serial1.println(String("Pan steps    : ") + (keyframe_array[index - 1].panStepCount));
  Serial1.println(String("Tilt steps   : ") + (keyframe_array[index - 1].tiltStepCount));
  Serial1.println(String("Slider steps : ") + (keyframe_array[index - 1].sliderStepCount));
  Serial1.println("#$");

  if (useKeyframeSpeeds) {
    stepper_pan.setMaxSpeed(keyframe_array[index - 1].panTiltSpeed);
    stepper_tilt.setMaxSpeed(keyframe_array[index - 1].panTiltSpeed);
    stepper_slider.setMaxSpeed(keyframe_array[index - 1].sliderSpeed);
  }

  //stepper_zoom.setMaxSpeed(pantilt_set_speed);

  stepper_pan.setTargetAbs(keyframe_array[index - 1].panStepCount);
  stepper_tilt.setTargetAbs(keyframe_array[index - 1].tiltStepCount);
  stepper_slider.setTargetAbs(keyframe_array[index - 1].sliderStepCount);
  stepper_zoom.setTargetAbs(keyframe_array[index - 1].zoomStepCount);

  isMoving = true;
  StepperGroup ({stepper_pan, stepper_tilt, stepper_slider, stepper_zoom}).move();

  Serial1Flush(); 
  Serial2Flush(); 

  if (index == 1) {
    atIndex = "#z";
    atPos1 = true;
    atPos2 = false;
    atPos3 = false;
    atPos4 = false;
    atPos5 = false;
    atPos6 = false;
    atPos7 = false;
    atPos8 = false;
    atPos9 = false;
    atPos0 = false;
  } else if (index == 2) {
    atIndex = "#x";
    atPos2 = true;
    atPos1 = false;
    atPos3 = false;
    atPos4 = false;
    atPos5 = false;
    atPos6 = false;
    atPos7 = false;
    atPos8 = false;
    atPos9 = false;
    atPos0 = false;
  } else if (index == 3) {
    atIndex = "#c";
    atPos3 = true;
    atPos1 = false;
    atPos2 = false;
    atPos4 = false;
    atPos5 = false;
    atPos6 = false;
    atPos7 = false;
    atPos8 = false;
    atPos9 = false;
    atPos0 = false;
  } else if (index == 4) {
    atIndex = "#v";
    atPos4 = true;
    atPos1 = false;
    atPos2 = false;
    atPos3 = false;
    atPos5 = false;
    atPos6 = false;
    atPos7 = false;
    atPos8 = false;
    atPos9 = false;
    atPos0 = false;
  } else if (index == 5) {
    atIndex = "#b";
    atPos5 = true;
    atPos1 = false;
    atPos2 = false;
    atPos3 = false;
    atPos4 = false;
    atPos6 = false;
    atPos7 = false;
    atPos8 = false;
    atPos9 = false;
    atPos0 = false;
  } else if (index == 6) {
    atIndex = "#n";
    atPos6 = true;
    atPos1 = false;
    atPos2 = false;
    atPos3 = false;
    atPos4 = false;
    atPos5 = false;
    atPos7 = false;
    atPos8 = false;
    atPos9 = false;
    atPos0 = false;
  } else if (index == 7) {
    atIndex = "#m";
    atPos7 = true;
    atPos1 = false;
    atPos2 = false;
    atPos3 = false;
    atPos4 = false;
    atPos5 = false;
    atPos6 = false;
    atPos8 = false;
    atPos9 = false;
    atPos0 = false;
  } else if (index == 8) {
    atIndex = "#,";
    atPos8 = true;
    atPos1 = false;
    atPos2 = false;
    atPos3 = false;
    atPos4 = false;
    atPos5 = false;
    atPos6 = false;
    atPos7 = false;
    atPos9 = false;
    atPos0 = false;
  } else if (index == 9) {
    atIndex = "#.";
    atPos9 = true;
    atPos1 = false;
    atPos2 = false;
    atPos3 = false;
    atPos4 = false;
    atPos5 = false;
    atPos6 = false;
    atPos7 = false;
    atPos8 = false;
    atPos0 = false;
  } else if (index == 10) {
    atIndex = "#/";
    atPos0 = true;
    atPos1 = false;
    atPos2 = false;
    atPos3 = false;
    atPos4 = false;
    atPos5 = false;
    atPos6 = false;
    atPos7 = false;
    atPos8 = false;
    atPos9 = false;
  }

  //delay(100);     // delay for serial read

  Serial1.println(atIndex);
  Serial1.println(atIndex);
  Serial2.println(atIndex);
  Serial1.println(String("At index: ") + index + String("\n"));
  Serial1.println("#$");

  if (useKeyframeSpeeds) {
    stepper_pan.setMaxSpeed(panDegreesToSteps(pantilt_set_speed));
    stepper_tilt.setMaxSpeed(tiltDegreesToSteps(pantilt_set_speed));
    stepper_slider.setMaxSpeed(sliderMillimetresToSteps(slider_set_speed));
  }

  //stepper_zoom.setMaxSpeed(zoom_set_speed);
  isMoving = false;
  sentMoved = false;
}