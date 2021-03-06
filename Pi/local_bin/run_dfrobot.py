#!/usr/bin/python
import os
import shutil
import sys
import thread
import cv2
import numpy as np
import urllib
import argparse
import re
import time
import compass
import logging
from logging import Formatter
from logging.handlers import RotatingFileHandler
import communication
import personal_assistant
import own_util

# General constants.
ImgWidth = 800
ImgHeight = 600
FpsLq = 2
FpsHq = 10
DirectionFront = 293.0
DirectionRight = 25.0
DirectionBack = 115.0
DirectionLeft = 203.0
# Constants which depend on the image format.
ImgWidthFactor = ImgWidth / 640.0  # Calibrated with 640 * 480 image.
ImgHeightFactor = ImgHeight / 480.0  # Calibrated with 640 * 480 image.
ImgAreaFactor = (ImgWidth * ImgHeight) / (640.0 * 480.0)  # Calibrated with 640 * 480 image.
# Blob detection constants
SizeMinForCorrection = 30.0 * ImgWidthFactor  # Calibrated with 640 * 480 image.
SizeMaxForCorrection = 40.0 * ImgWidthFactor  # Calibrated with 640 * 480 image.
SizeSlow = 30.0 * ImgWidthFactor  # Calibrated with 640 * 480 image.
SizeStop = 60.0 * ImgWidthFactor  # Calibrated with 640 * 480 image.
# Motion detection constants.
MotionDetectionBufferLength = FpsLq * 30  # Number of images in motion detection buffer.
MotionDetectionBufferOffset = FpsLq * 3   # Number of images that are kept before the motion is detected.
GrayLevelDifferenceTreshold = 80        # The larger this number the larger the graylevel difference must be to be considered as true motion.
MinContourArea = 100 * ImgAreaFactor    # The larger this number the larger the motion contours must be to be considerd as true motion.
MaxNofContours = 200                    # Maximum number of contours otherwise it will not be considered as true motion.
# Upload constants.
NofMotionVideosToKeep = 10
NofHomeRunVideosToKeep = 3

# Global variables.
globMyLog = None
globBrightness = 0

# Initialization.
doPrint = False
doTestMotion = False
doShow = False
doMove = True
logFilePath = ''


def getNewImage():
    global globContinueCapture, globBytes, globStream, globImg, globNewImageAvailable, globNewImageAvailableLock
    global globBrightness

    while globContinueCapture == True:
        globBytes+=globStream.read(1024)
        a = globBytes.find('\xff\xd8')
        b = globBytes.find('\xff\xd9')
        if a!=-1 and b!=-1:
            jpg = globBytes[a:b+2]
            globBytes= globBytes[b+2:]

            img = cv2.imdecode(np.fromstring(jpg, dtype=np.uint8),cv2.CV_LOAD_IMAGE_COLOR)
            # Get average brightness of hsv image by averaging the 'v' (value or brightness) bytes.
            img_hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            totalPixel = cv2.sumElems(img_hsv)
            globBrightness = totalPixel[2] / (ImgWidth * ImgHeight)
            if doPrint:
                print 'brightness:', globBrightness
            # Keep critical section as short as possible.
            globNewImageAvailableLock.acquire()
            globImg = img
            globNewImageAvailable = True
            globNewImageAvailableLock.release()
    # Close the stream to have a correct administration of the number of connections.
    globStream.close()


def homeRun():
    global globContinueCapture, globBytes, globStream, globImg, globNewImageAvailable, globNewImageAvailableLock
    global globBrightness

    globStream=urllib.urlopen('http://@localhost:44445/?action=stream')
    globBytes=''
    globNewImageAvailable = False
    globNewImageAvailableLock = thread.allocate_lock()
    globContinueCapture = True
    thread.start_new_thread(getNewImage, ())

    correctApproachAngle = False
    correction = 0
    imgCount = 0

    # Remove tmp_img and tmp_tmp_img files to be sure no tmp images are left from a previous run.
    stdOutAndErr = own_util.runShellCommandWait('rm -f /home/pi/DFRobotUploads/tmp_*img*')
    globMyLog.info(stdOutAndErr)

    # Start with cam down.
    own_util.moveCamAbs(0, 0.1)
    # Switch on light if needed
    if globBrightness < 60:
        own_util.switchLight(True)

    # Indicate that a Home run is started.
    # A Home run can be stopped by setting own_util.globStop to True.
    own_util.globDoHomeRun = True
    while globContinueCapture == True and own_util.globStop == False:
        # Keep critical section as short as possible.
        globNewImageAvailableLock.acquire()
        newImageAvailable = globNewImageAvailable
        if newImageAvailable:
            img = globImg
            globNewImageAvailableLock.release()
            img_gray = cv2.cvtColor(img, cv2.cv.CV_BGR2GRAY)

            # Setup SimpleBlobDetector parameters.
            params = cv2.SimpleBlobDetector_Params()

            # Change thresholds
            # The default value of params.thresholdStep (10?) seems to work well.
            # To speed processing up, increase to 20 or more.
            #params.thresholdStep = 20
            params.minThreshold = 20
            params.maxThreshold = 200

            # Filter by Area.
            # This prevents that many small blobs (one pixel) will be detected.
            # In addition tt is observed that in that case invalid keypoint coordinates are produced: nan (not a number).
            # When filterByArea is set to True with a minArea > 0 this problem does not occur.
            params.filterByArea = True
            params.minArea = 100 * ImgAreaFactor
            params.maxArea = 100000 * ImgAreaFactor

            # Filter by Circularity
            params.filterByCircularity = True
            params.minCircularity = 0.80

            # Filter by Convexity
            params.filterByConvexity = False
            params.minConvexity = 0.87

            # Filter by Inertia
            params.filterByInertia = False
            params.minInertiaRatio = 0.01

            #Filter by distance between blobs
            #params.minDistBetweenBlobs = 100

            # Detect blobs.
            detector = cv2.SimpleBlobDetector(params)
            blobs = detector.detect(img_gray)

            # Sort blobs on horizontal position and check if they are valid.
            sortedBlobs = sorted(blobs, key=lambda x: x.pt[0], reverse=False)
            blobLeft = None
            blobMiddle = None
            blobRight = None
            validBlobsFound = False
            for blob in sortedBlobs:
                # Fill in three blobs, left, middle, right.
                if blobLeft is None:
                    blobLeft = blob
                elif blobMiddle is None:
                    # Check if there is a middle blob found which is 3 times smaller than the left blob.
                    if blob.size > blobLeft.size/4.0 and blob.size < blobLeft.size/2.0:
                        blobMiddle = blob
                        continue
                if blobRight is None:
                    # Skip blop if it is significantly smaller than the first blob.
                    if blob.size < blobLeft.size/2.0:
                        continue
                    blobRight = blob
                    # We have two or three blobs now, check if these are valid
                    # For now we consider the blobs valid if the left and right one have appr. equal size.
                    # The size of a blob is radius in pixels.
                    distBlobLeftBlobRight = blobRight.pt[0] - blobLeft.pt[0]
                    avgSizeBlobLeftBlobRight = (blobLeft.size + blobRight.size) / 2.0
                    if abs((blobLeft.size - blobRight.size) / avgSizeBlobLeftBlobRight) < 0.3 and abs((distBlobLeftBlobRight - avgSizeBlobLeftBlobRight * 3.0) / ((distBlobLeftBlobRight + avgSizeBlobLeftBlobRight * 3.0) / 2.0)) < 0.3:
                        validBlobsFound = True
                    else:
                        if doPrint:
                            if blobMiddle is not None:
                                print 'Blob conditions not met, left:', blobLeft.pt[0], blobLeft.size, 'middle:', blobMiddle.pt[0], blobMiddle.size, 'right:', blobRight.pt[0], blobRight.size, 'distBlobLeftBlobRight:', distBlobLeftBlobRight
                            else:
                                print 'Blob conditions not met, left:', blobLeft.pt[0], blobLeft.size, 'right:', blobRight.pt[0], blobRight.size, 'distBlobLeftBlobRight:', distBlobLeftBlobRight
                    if validBlobsFound:
                        # We have found two or three valid blobs, break out of loop.
                        break
                    else:
                        # No valid blobs found yet, shift one blob up.
                        # We assume that valid blobs are adjacent.
                        # This is reasonable as the real blobs will indeed be close to each other.
                        if blobMiddle is not None:
                            blobLeft = blobMiddle
                            blobMiddle = blobRight
                            blobRight = None
                        else:
                            blobLeft = blobRight
                            blobRight = None

            if correctApproachAngle:
                # Going to check and correct the approach angle.
                if correction > 1.5:  # we have to turn to the left, move forward and then turn back again
                    if doPrint:
                        print '********** Going to do approach correction to the left.'
                    # Rotate left 90 degrees minus a correction dependant on the sideways displacement.
                    own_util.move('left', 112 - correction * 1, 1.0, doMove)
                    own_util.move('forward', correction * 3, 1.0, doMove)
                    # Rotate back 90 degrees towards target. We do not use a correction here because we have to rotate a bit further back to the target.
                    own_util.move('right', 112, 5.0, doMove)
                    # approach correction finished
                    correctApproachAngle = False
                    if doPrint:
                        print 'Approach correction Finished.'
                elif correction < -1.5:  # we have to turn to the right, move forward and then turn back again
                    if doPrint:
                        print 'Going to do approach correction to the right.'
                    # Rotate right 90 degrees minus a correction dependant on the sideways displacement.
                    own_util.move('right', 112 + correction * 1, 1.0, doMove)
                    own_util.move('forward', -correction * 3, 1.0, doMove)
                    # Rotate back 90 degrees towards target. We do not use a correction here because we have to rotate a bit further back to the target.
                    own_util.move('left', 112, 5.0, doMove)
                    # approach correction finished
                    correctApproachAngle = False
                    if doPrint:
                        print 'Approach correction finished.'

            elif validBlobsFound:
                if doPrint:
                    print '**********', len(sortedBlobs), 'Valid blobs found!'
                    if blobMiddle is not None:
                        print 'left:', blobLeft.pt[0], blobLeft.size, 'middle:', blobMiddle.pt[0], blobMiddle.size, 'right:', blobRight.pt[0], blobRight.size, 'distBlobLeftBlobRight:', distBlobLeftBlobRight
                    else:
                        print 'left:', blobLeft.pt[0], blobLeft.size, 'right:', blobRight.pt[0], blobRight.size, 'distBlobLeftBlobRight:', distBlobLeftBlobRight
                # Go home!
                xmid = (blobLeft.pt[0] + blobRight.pt[0]) / 2.0
                ymid = (blobLeft.pt[1] + blobRight.pt[1]) / 2.0
                # Move cam to vertically center the target.
                own_util.moveCamRel(30 * (ImgHeight/2 - ymid) / ImgHeight, 0.1)
                course = ImgWidth / 2.0
                if blobMiddle is not None:
                    correction = (xmid - blobMiddle.pt[0]) / ImgWidthFactor
                else:
                    correction = 0
                if doPrint:
                    print 'xmid, course, correction:', xmid, course, correction
                if xmid < course - ImgWidth / 20.0:
                    if doPrint:
                        print 'turn left'
                    if xmid < course - ImgWidth / 5.0:
                        own_util.move('left', 12, 1.0, doMove)
                    else:
                        own_util.move('left', 1, 1.0, doMove)
                elif xmid > course + ImgWidth / 20.0:
                    if doPrint:
                        print 'turn right'
                    if xmid > course + ImgWidth / 5.0:
                        own_util.move('right', 12, 1.0, doMove)
                    else:
                        own_util.move('right', 1, 1.0, doMove)
                elif abs(correction) > 2.0 and avgSizeBlobLeftBlobRight > SizeMinForCorrection and avgSizeBlobLeftBlobRight < SizeMaxForCorrection:
                    correctApproachAngle = True
                else:
                    if avgSizeBlobLeftBlobRight < SizeStop:
                        if doPrint:
                            print 'move forward'
                        if avgSizeBlobLeftBlobRight < SizeSlow:
                            own_util.move('forward', 32, 1.0, doMove)
                        else:
                            own_util.move('forward', 12, 1.0, doMove)
                    else:
                        # Make one more additional move towards the garage before turning 180 degrees.
                        # Switch off the light relay as its magnetic field influences the compass (can be 10 degrees difference)!!
                        own_util.switchLight(False)
                        own_util.move('forward',20, 1.0, doMove)
                        compass.gotoDegreeRel(180, doMove)
                        for i in range(0, 8):
                            own_util.move('backward', 12, 1.0, doMove)
                        globMyLog.info('Home found!')
                        globContinueCapture = False

            elif len(sortedBlobs) > 0:
                if doPrint:
                    print '**********', len(sortedBlobs), 'Blobs found, but not valid.'
                    print 'turn left'
                own_util.move('left', 22, 1.0, doMove)
            else:
                if doPrint:
                    print '********** No blobs found.'
                    print 'turn left'
                own_util.move('left', 22, 1.0, doMove)

            for blob in sortedBlobs:
                x = blob.pt[0]
                y = blob.pt[1]
                cv2.circle(img, (int(x), int(y)), int(blob.size), (0, 255, 0), 2)

            # Write images with name like 'tmp_img000042.jpg'.
            # Use leading zeros to make sure order is correct when using shell filename expansion.
            cv2.imwrite('/home/pi/DFRobotUploads/tmp_img' + str(imgCount).zfill(6) + '.jpg', img)

            if doShow:
                # Show keypoints
                cv2.imshow("Keypoints", img)
                cv2.waitKey(100)

            # Increase imgCount with a maximum for protection.
            imgCount = (imgCount + 1) % (FpsLq * 300)

            # Ready with movement. Make globNewImageAvailable false to make sure a new image is taken after movement.
            # Keep critical section as short as possible.
            globNewImageAvailableLock.acquire()
            globNewImageAvailable = False
            globNewImageAvailableLock.release()
        else:
            globNewImageAvailableLock.release()

    # Stop the getNewImage thread and indicate Home run is finished.
    globContinueCapture = False
    own_util.globDoHomeRun = False
    # Move cam down again.
    own_util.moveCamAbs(0, 0.1)
    # Switch off light if it was on.
    own_util.switchLight(False)
    # Convert the Home run images to a video and remove the images.
    stdOutAndErr = own_util.runShellCommandWait('mencoder mf:///home/pi/DFRobotUploads/tmp_img*.jpg -mf w=' + str(ImgWidth) + ':h=' + str(ImgHeight) + ':fps=' + str(FpsLq) + ':type=jpg -ovc lavc -lavcopts vcodec=mpeg4:mbd=2:trell -oac copy -o /home/pi/DFRobotUploads/dfrobot_video.avi')
    globMyLog.info(stdOutAndErr)
    # Remove tmp_img and tmp_tmp_img files.
    stdOutAndErr = own_util.runShellCommandWait('rm -f /home/pi/DFRobotUploads/tmp_*img*')
    globMyLog.info(stdOutAndErr)


def captureAndMotionDetection():
    global globContinueCapture, globBytes, globStream, globImg, globNewImageAvailable, globNewImageAvailableLock
    global globBrightness

    globStream=urllib.urlopen('http://@localhost:44445/?action=stream')
    globBytes=''
    globNewImageAvailable = False
    globNewImageAvailableLock = thread.allocate_lock()
    globContinueCapture = True
    thread.start_new_thread(getNewImage, ())

    img = img_gray = img_gray_prev = None
    imgCount = 0
    motionDetected = prevMotionDetected = False
    noOfConsecutiveMotions = 0
    firstImageIndex = 0

    # Remove tmp_img and tmp_tmp_img files to be sure no tmp images are left from a previous run.
    stdOutAndErr = own_util.runShellCommandWait('rm -f /home/pi/DFRobotUploads/tmp_*img*')
    globMyLog.info(stdOutAndErr)

    logCount = 0
    pictureCountDown = 0
    lightSwitchedOn = False
    while globContinueCapture == True:
        if communication.globWebSocketInteractive == True or personal_assistant.globInteractive == True:
            if doPrint:
                print 'stopping capture and motion detection because the interactive mode is active'
            globMyLog.info('stopping capture and motion detection because the interactive mode is active')
            globContinueCapture = False
            return False

        # Keep critical section as short as possible.
        globNewImageAvailableLock.acquire()
        newImageAvailable = globNewImageAvailable
        if newImageAvailable:
            img = globImg
            globNewImageAvailableLock.release()

            # Check if picture has to be sent to Telegram.
            if personal_assistant.globTelegramSendPicture == True:
                # pictureCountDown is used in case it is dark and the ligth has to be switched on.
                # It counts down a number of images to give the camera time to settle. When pictureCountDown == 0, the picture is taken.
                # Switch on light if needed.
                if globBrightness < 60 and lightSwitchedOn == False:
                    own_util.switchLight(True)
                    lightSwitchedOn = True
                    pictureCountDown = 10
                # Save img to latest_img.jpg and send it with Telegram.
                if pictureCountDown == 0:
                    cv2.imwrite('/home/pi/DFRobotUploads/latest_img.jpg', img)
                    communication.sendTelegramImg('/home/pi/DFRobotUploads/latest_img.jpg', 'Here is your picture!')
                    # Not needed to lock here as it is ok to miss a 'send picture' command when they come in too fast.
                    personal_assistant.globTelegramSendPicture = False
                    # Switch off light.
                    own_util.switchLight(False)
                    lightSwitchedOn = False
                else:
                    pictureCountDown = pictureCountDown - 1

            # If globDoMotionDetection == False, then only capture images for sending pictures if required.
            if personal_assistant.globDoMotionDetection == False:
                if logCount == 0:
                    if doPrint:
                        print 'globDoMotionDetection set to False'
                    globMyLog.info('globDoMotionDetection set to False')
                    logCount = 1
                # Reset values for the next time motion detection is switched on.
                img = img_gray = img_gray_prev = None
                imgCount = 0
                motionDetected = prevMotionDetected = False
                noOfConsecutiveMotions = 0
                firstImageIndex = 0
            else:
                # Motion detection
                if logCount == 1:
                    if doPrint:
                        print 'globDoMotionDetection set to True'
                    globMyLog.info('globDoMotionDetection set to True')
                    logCount = 0

                if img_gray is not None:
                    img_gray_prev = img_gray.copy()
                img_gray = cv2.cvtColor(img, cv2.cv.CV_BGR2GRAY)
                img_gray = cv2.GaussianBlur(img_gray, (21, 21), 0)


                if img_gray_prev is not None:
                    img_gray_diff = cv2.absdiff(img_gray, img_gray_prev)
                    img_bw_diff = cv2.threshold(img_gray_diff, GrayLevelDifferenceTreshold, 255, cv2.THRESH_BINARY)[1]
                    (cnts, _) = cv2.findContours(img_bw_diff.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

                    if doPrint:
                        print 'number of contours:', len(cnts)

                    # Loop over the contours.
                    # If number of contours is too high it is not considered true motion.
                    if len(cnts) < MaxNofContours:
                        # xLeft, xRight, yTop, yBottom will be the coordinates of the outer bounding box of all contours.
                        xLeft = ImgWidth
                        xRight = 0
                        yTop = ImgHeight
                        yBottom = 0
                        nofValidContours = 0
                        for c in cnts:
                            # If the contour is too small, ignore it.
                            if cv2.contourArea(c) > MinContourArea:
                                # Compute the outer bounding box of all valid contours.
                                nofValidContours = nofValidContours + 1
                                x,y,w,h = cv2.boundingRect(c)
                                xLeft = x if x < xLeft else xLeft
                                xRight = x+w if x+w > xRight else xRight
                                yTop = y if y < yTop else yTop
                                yBottom = y+h if y+h > yBottom else yBottom
                                if doShow:
                                    # Only with -show option draw all the contours.
                                    cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)
                        if nofValidContours > 0:
                            totalArea = (xRight - xLeft) * (yBottom - yTop)
                            if doPrint:
                                print 'total area:', totalArea
                            if totalArea < (ImgWidth * ImgHeight) * 0.5:
                                # Motion is detected for this image.
                                # Consider true motion detected only after sufficient images in sequence with motion.
                                noOfConsecutiveMotions = noOfConsecutiveMotions + 1
                                if noOfConsecutiveMotions >= 3:
                                    if doPrint:
                                        print '******************** MOTION DETECTED! ********************'
                                    # Draw the outer bounding box of all contours.
                                    cv2.rectangle(img, (xLeft, yTop), (xRight, yBottom), (0, 255, 255), 2)
                                    if doTestMotion == False:
                                        motionDetected = True
                            else:
                                # Reset, images with motion have to be in sequence.
                                noOfConsecutiveMotions = 0

                # Write images with name like 'tmp_img000042.jpg'.
                # Use leading zeros to make sure order is correct when using shell filename expansion.
                firstImageName = '/home/pi/DFRobotUploads/tmp_tmp_img' + str(imgCount).zfill(6) + '.jpg'
                cv2.imwrite(firstImageName, img)

                if motionDetected == True:
                    # Motion is detected,
                    # now acquire MotionDetectionBufferLength - MotionDetectionBufferOffset new images.
                    # First determine where we are in the circular buffer.
                    if prevMotionDetected == False and motionDetected == True:
                        # Send  motion image or text to Telegram. Do it here so it will arrive fast!
                        #if doPrint:
                        #    print 'motion detected, going to send motion picture to Telegram'
                        #globMyLog.info('motion detected, going to send motion picture to Telegram')
                        # Line below commented out, motion video is sent instead.
                        #communication.sendTelegramImg(firstImageName, 'Motion detected!')

                        firstImageIndex = imgCount
                        extraImgCount = 0
                        prevMotionDetected = True
                    else:
                        extraImgCount = extraImgCount + 1
                        if doPrint:
                            print 'capturing extra image no:', extraImgCount

                        if extraImgCount == MotionDetectionBufferLength - MotionDetectionBufferOffset - 1:
                            # All required images for this motion are captured, stop the capturing.
                            globContinueCapture = False

                if doShow:
                    # Show motion
                    cv2.imshow("Motion", img)
                    cv2.waitKey(100)

                # imgCount keeps position in circular buffer.
                imgCount = (imgCount + 1) % MotionDetectionBufferLength

            # Ready with this image. Make globNewImageAvailable false to make sure a new image is taken.
            # Keep critical section as short as possible.
            globNewImageAvailableLock.acquire()
            globNewImageAvailable = False
            globNewImageAvailableLock.release()
        else:
            globNewImageAvailableLock.release()

    # Motion detection loop is finished. The images are in a circular buffer and the first image with
    # motion is at firstImageIndex. Before this image there are MotionDetectionBufferOffset images
    # before the motion.
    # Before we can make a movie we have to shift the motion detection images so the preamble
    # starts at index 0.
    for i in range(0, MotionDetectionBufferLength):
        # Rename images such that tmp_img000000.jpg is the first image to show in the movie.
        # Note that this is MotionDetectionBufferOffset images before motion is detected.
        iOffset = i - (firstImageIndex - MotionDetectionBufferOffset)
        # Map iOffset back into circular buffer.
        if iOffset < 0:
            iOffset = iOffset + MotionDetectionBufferLength
        elif iOffset >= MotionDetectionBufferLength:
            iOffset = iOffset - MotionDetectionBufferLength
        # Rename the tmp_tmp_img file with index i to tmp_img files with the correct index iOffset.
        # As we are not sure all MotionDetectionBufferLength tmp_tmp_img* images are really created, we accept IOError exceptions here.
        try:
            shutil.move('/home/pi/DFRobotUploads/tmp_tmp_img' + str(i).zfill(6) + '.jpg', '/home/pi/DFRobotUploads/tmp_img' + str(iOffset).zfill(6) + '.jpg')
        except IOError:
            pass
    # Motion detection images are shifted now. Convert the images to a video and remove the images.
    stdOutAndErr = own_util.runShellCommandWait('mencoder mf:///home/pi/DFRobotUploads/tmp_img*.jpg -mf w=' + str(ImgWidth) + ':h=' + str(ImgHeight) + ':fps=' + str(FpsLq) + ':type=jpg -ovc lavc -lavcopts vcodec=mpeg4:mbd=2:trell -oac copy -o /home/pi/DFRobotUploads/dfrobot_video.avi')
    globMyLog.info(stdOutAndErr)
    # Remove tmp_img and tmp_tmp_img files.
    stdOutAndErr = own_util.runShellCommandWait('rm -f /home/pi/DFRobotUploads/tmp_*img*')
    globMyLog.info(stdOutAndErr)
    return True


def createMyLog(path):
    global globMyLog
    globMyLog = logging.getLogger("MyLog")
    globMyLog.setLevel(logging.INFO)

    # add a rotating handler
    FORMAT = '%(asctime)-15s %(levelname)-6s %(message)s'
    DATE_FORMAT = '%b %d %H:%M:%S'
    formatter = Formatter(fmt=FORMAT, datefmt=DATE_FORMAT)
    handler = RotatingFileHandler(path, maxBytes=1000000, backupCount=0)
    handler.setFormatter(formatter)
    globMyLog.addHandler(handler)

# Main script.
# This script can run the robot in different modes:
# Default run:
#   The robot does motion detection and sends a motion video to Telegram when motion is detected.
#   Once every hour the robot drives out of its garage, makes an exploratory round
#   and returns to the garage where it makes connection with the charging station.
#   This video is also sent to Telegram.
# Home run:
#   The robot drives back to the garage where it makes connection with the charging station.

# Handle arguments.
parser = argparse.ArgumentParser()
parser.add_argument('--log', default='/home/pi/log/dfrobot_runlog.txt')
parser.add_argument('--doprint', action='store_true')
parser.add_argument('--testmotion', action='store_true')
parser.add_argument('--show', action='store_true')
parser.add_argument('--nomove', action='store_true')
args = parser.parse_args()

logFilePath = args.log
if args.doprint:
    doPrint = True
if args.testmotion:
    doTestMotion = True
    doPrint = True
    personal_assistant.globDoMotionDetection = True
if args.show:
    doShow = True
if args.nomove:
    doMove = False

# Create logger.
createMyLog(logFilePath)
globMyLog.info('START LOG  *****')

# Start Telegram client.
communication.startTelegramClient()
communication.sendTelegramMsg('I am up and running!')

# Start websocket server.
communication.startWebSocketServer()

# Start personal assistant.
personal_assistant.startPersonalAssistant()

# Start status update thread.
thread.start_new_thread(communication.statusUpdateThread, ())
streamStarted = False

# FPV vatiables
# Take rounded values of maxSpeed and minSpeed such that rounded increments and decrements will pass the '0' value so we can stop the robot exactly.
maxSpeed = 62
minSpeed = -62
turnSpeedFactorWhenStandingStill = 1.5
prevSpeedStraight = 0
lastTimeInteractiveMode = 0
lastTimeWsConnectionAlive = 0
while True:
    # Catch exceptions and log them.
    try:
        # Short sleep to preempt this thread. Otherwise this thread will be dominating and other threads.
        time.sleep(0.001)
        
        if communication.globWebSocketInteractive == True or personal_assistant.globInteractive == True:
            # Interactive mode.
            # Check if FPV mode is active and if FPV connection with robot is still alive. If not stop motors!
            if lastTimeWsConnectionAlive != 0:
                if time.time() - lastTimeWsConnectionAlive > 1.0: # If time since last alive message > 1 second.
                    globMyLog.info('FPV connection timeout, stopping motors')
                    own_util.driveAndTurn(0, 0, 0, 0, 0, doMove)   # Stop motors.
                    prevSpeedStraight = 0
                    # Motors are stopped, set lastTimeWsConnectionAlive to 0 to stop checking until the next FPV cmd is received.
                    lastTimeWsConnectionAlive = 0

            # Intearctive command received. This command looks like "drive-and-turn.0.31".
            # The below regular expression will separate the command on the dots so the result will be an tuple ["drive-and-turn", "0", "31"].
            expr = re.compile('(.+?)(?:$|\.)')
            cmdList = []
            if communication.globWebSocketInteractive == True and len(cmdList) == 0: # Only execute when len(cmdList) == 0 meaning still no valid cmd received.
                cmdList = expr.findall(communication.globWebSocketInMsg)
            if personal_assistant.globInteractive == True and len(cmdList) == 0:     # Only execute when len(cmdList) == 0 meaning still no valid cmd received.
                cmdList = expr.findall(personal_assistant.globCmd)
            # Now cmdList is a a list containing the cmd and its parameters. The cmdList[0] contains the command.
            # If there are no parameters len(cmdList) == 1.
            if len(cmdList) > 0:
                # Valid command received meaning the interactive mode is (still) active, so update lastTimeInteractiveMode.
                lastTimeInteractiveMode = time.time()
                if doPrint:
                    print 'command received:', str(cmdList)
                globMyLog.info('command received: ' + str(cmdList))
                if cmdList[0] == 'start-stream-hq':
                    # Start MJPEG stream. Stop previous stream first if any. Use sudo because stream can be started by another user.
                    stdOutAndErr = own_util.runShellCommandWait('sudo killall mjpg_streamer')
                    globMyLog.info('going to start hq stream')
                    if doPrint:
                        print 'going to start hq stream'
                    time.sleep(0.5)
                    own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_raspicam.so -vf -hf -fps ' + str(FpsHq) + ' -q 10 -x ' + str(ImgWidth) + ' -y '+ str(ImgHeight) + '" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                    # Resolution of PU` Aimetis HD USB Camera Module: 2560x960 or 1280x480. Set contrast (co) and sharpness (sh), range 0..100.
                    #own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_uvc.so -d /dev/video0 -f 20 -r 1280x480 -co 50 -sh 100" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                elif cmdList[0] == 'start-stream-lq':
                    # Start MJPEG stream. Stop previous stream first if any. Use sudo because stream can be started by another user.
                    stdOutAndErr = own_util.runShellCommandWait('sudo killall mjpg_streamer')
                    globMyLog.info('going to start lq stream')
                    if doPrint:
                        print 'going to start lq stream'
                    time.sleep(0.5)
                    own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_raspicam.so -vf -hf -fps ' + str(FpsLq) + ' -q 10 -x ' + str(ImgWidth) + ' -y '+ str(ImgHeight) + '" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                    # Resolution of PU` Aimetis HD USB Camera Module: 2560x960 or 1280x480. Set contrast (co) and sharpness (sh), range 0..100.
                    #own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_uvc.so -d /dev/video0 -f 20 -r 1280x480 -co 50 -sh 100" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                elif cmdList[0] == 'start-fpv':
                    # Start MJPEG stream. Stop previous stream first if any. Use sudo because stream can be started by another user.
                    stdOutAndErr = own_util.runShellCommandWait('sudo killall mjpg_streamer')
                    globMyLog.info('going to start fpv stream')
                    if doPrint:
                        print 'going to start fpv stream'
                    time.sleep(0.5)
                    own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_raspicam.so -vf -hf -fps ' + str(FpsHq) + ' -q 10 -x ' + str(ImgWidth) + ' -y '+ str(ImgHeight) + '" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                    # Resolution of PU` Aimetis HD USB Camera Module: 2560x960 or 1280x480. Set contrast (co) and sharpness (sh), range 0..100.
                    #own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_uvc.so -d /dev/video0 -f 20 -r 1280x480 -co 50 -sh 100" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                elif cmdList[0] == 'stop-fpv':
                    # Start MJPEG stream. Stop previous stream first if any. Use sudo because stream can be started by another user.
                    stdOutAndErr = own_util.runShellCommandWait('sudo killall mjpg_streamer')
                    globMyLog.info('going to start lq stream')
                    if doPrint:
                        print 'going to start lq stream'
                    time.sleep(0.5)
                    own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_raspicam.so -vf -hf -fps ' + str(FpsLq) + ' -q 10 -x ' + str(ImgWidth) + ' -y '+ str(ImgHeight) + '" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                    # Resolution of PU` Aimetis HD USB Camera Module: 2560x960 or 1280x480. Set contrast (co) and sharpness (sh), range 0..100.
                    #own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_uvc.so -d /dev/video0 -f 20 -r 1280x480 -co 50 -sh 100" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                elif cmdList[0] == 'stop-stream':
                    # Stop stream. Use sudo because stream can be started by another user.
                    globMyLog.info('going to stop stream')
                    if doPrint:
                        print 'going to stop stream'
                    stdOutAndErr = own_util.runShellCommandWait('sudo killall mjpg_streamer')
                elif cmdList[0] == 'capture-start':
                    # Start capture video from http stream, with timeout of 60 seconds.
                    globMyLog.info('going to start capture http MJPEG stream')
                    if doPrint:
                        print 'going to start capture http MJPEG stream'
                    # Command below is commented out but left here for reference.
                    #own_util.runShellCommandNowait('cvlc http://localhost:44445/?action=stream --sout \'#transcode{vcodec=h264,vb=0,scale=0,acodec=mp3,ab=128,channels=2,samplerate=44100}:file{dst=/home/pi/DFRobotUploads/dfrobot_video.mp4}\' --run-time=60 vlc://quit')
                    own_util.runShellCommandNowait('cvlc http://localhost:44445/?action=stream --sout=file/avi:/\'home/pi/DFRobotUploads/dfrobot_video.avi\' --run-time=60 vlc://quit')
                elif cmdList[0] == 'capture-stop':
                    # Stop capture video from http stream.
                    globMyLog.info('going to stop capture http MJPEG stream')
                    if doPrint:
                        print 'going to stop capture http MJPEG stream'
                    # Use pkill -f with regular expression to kill te right vlc process.
                    stdOutAndErr = own_util.runShellCommandWait('sudo pkill -f "vlc.*localhost:44445"')
                    # Send captured video to Telegram.
                    communication.sendTelegramVideo('/home/pi/DFRobotUploads/dfrobot_video.avi', 'Here is your captured video!')
                elif cmdList[0] == 'home-start':
                    # Home run.
                    globMyLog.info('going to start Home run')
                    if doPrint:
                        print 'Going to start Home run'
                    # Start MJPEG stream. Stop previous stream first if any. Use sudo because stream can be started by another user.
                    stdOutAndErr = own_util.runShellCommandWait('sudo killall mjpg_streamer')
                    globMyLog.info('going to start lq stream')
                    time.sleep(0.5)
                    own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_raspicam.so -vf -hf -fps ' + str(FpsLq) + ' -q 10 -x ' + str(ImgWidth) + ' -y '+ str(ImgHeight) + '" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                    # Resolution of PU` Aimetis HD USB Camera Module: 2560x960 or 1280x480. Set contrast (co) and sharpness (sh), range 0..100.
                    #own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_uvc.so -d /dev/video0 -f 20 -r 1280x480 -co 50 -sh 100" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                    # Delay to give stream time to start up and camera to stabilize.
                    time.sleep(5)
                    homeRun()
                    # Upload homerun video to Telegram.
                    communication.sendTelegramVideo('/home/pi/DFRobotUploads/dfrobot_video.avi', 'Here is your homerun video!')
                elif cmdList[0] in ['forward', 'backward', 'left', 'right']:
                    own_util.move(cmdList[0], int(cmdList[1]), 0, doMove)
                elif cmdList[0] == 'ws-alive':
                    lastTimeWsConnectionAlive = time.time()
                elif cmdList[0] == 'drive-inc':
                    # Calculate new speed and keep it between minSpeed and maxSpeed.
                    newSpeedStraight = max(min(prevSpeedStraight + int(cmdList[1]), maxSpeed), minSpeed)
                    own_util.driveAndTurn(newSpeedStraight, 0, 0, 0, 0, doMove) # Drive straight ahead.
                    prevSpeedStraight = newSpeedStraight
                    lastTimeWsConnectionAlive = time.time()
                elif cmdList[0] == 'turn-inc':
                    turnSpeed = int(cmdList[1])
                    if prevSpeedStraight == 0:
                        # When standing still a higher turning speed is needed.
                        turnSpeed = int(int(cmdList[1]) * turnSpeedFactorWhenStandingStill)
                    own_util.driveAndTurn(prevSpeedStraight, turnSpeed, 0, 60, 0, doMove)
                    lastTimeWsConnectionAlive = time.time()
                elif cmdList[0] == 'drive-and-turn':
                    own_util.driveAndTurn(cmdList[1], cmdList[2], cmdList[3], cmdList[4], 0, doMove)
                    lastTimeWsConnectionAlive = time.time()
                elif cmdList[0] == 'cam-move-rel':
                    own_util.moveCamRel(int(cmdList[1]), 0.1)
                elif cmdList[0] == 'cam-move-abs':
                    own_util.moveCamAbs(int(cmdList[1]), 0.1)
                elif cmdList[0] == 'light-on':
                    own_util.switchLight(True)
                elif cmdList[0] == 'light-off':
                    own_util.switchLight(False)
                elif cmdList[0] == 'demo-start':
                    # Switch on light if needed
                    if globBrightness < 60:
                        own_util.switchLight(True)
                    own_util.move('forward', 72, 1.0, doMove)
                    own_util.move('forward', 72, 1.0, doMove)
                    own_util.move('left', 32, 1.0, doMove)
                    own_util.move('forward', 72, 1.0, doMove)
                    # Demo Home run, no upload of video.
                    globMyLog.info('going to start Home run')
                    if doPrint:
                        print 'going to start Home run'
                    # Start MJPEG stream. Stop previous stream first if any. Use sudo because stream can be started by another user.
                    stdOutAndErr = own_util.runShellCommandWait('sudo killall mjpg_streamer')
                    globMyLog.info('going to start lq stream')
                    time.sleep(0.5)
                    own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_raspicam.so -vf -hf -fps ' + str(FpsLq) + ' -q 10 -x ' + str(ImgWidth) + ' -y '+ str(ImgHeight) + '" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                    # Resolution of PU` Aimetis HD USB Camera Module: 2560x960 or 1280x480. Set contrast (co) and sharpness (sh), range 0..100.
                    #own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_uvc.so -d /dev/video0 -f 20 -r 1280x480 -co 50 -sh 100" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                    # Delay to give stream time to start up and camera to stabilize.
                    time.sleep(5)
                    homeRun()
                    # Upload homerun video to Telegram.
                    communication.sendTelegramVideo('/home/pi/DFRobotUploads/dfrobot_video.avi', 'Here is your homerun video!')
                elif cmdList[0] == 'patrol-start':
                    globMyLog.info('going to start patrol')
                    startTimePatrol = time.time()
                    if doPrint:
                        print 'going to start patrol'
                # A stop command can interrupt a previous command like a Home run.
                # The stop command does not come in through the regular cmdList[0] but from own_util.globStop which is set by the input handlers.
                # This because the command handler here is busy with the previous command which has to be stopped.
                # After a previous command is stopped we check for the stop command to stop the motors, lights, etc..
                if own_util.globStop == True:
                    globMyLog.info('going to stop action')
                    if doPrint:
                        print 'going to stop action'
                    own_util.driveAndTurn(0, 0, 0, 0, 0, doMove)
                    own_util.switchLight(False)
                    # Indicate stop command has been handled.
                    own_util.globStop = False

                # Command handled, so make empty.
                communication.globWebSocketInMsg = ''
                personal_assistant.globCmd = ''
            else:
                # len(cmdList) == 0 so  no valid command is received.
                # Check if interactive mode is still active since the last minute, otherwise set it to inactive so the default mode (captureAndMotionDetection) can run again.
                if time.time() - lastTimeInteractiveMode > 60:
                    communication.globWebSocketInteractive = False
                    personal_assistant.globInteractive = False
    
        else:
            # Non interactive mode.
            # Switch to captureAndMotionDetection. This mode stops when there is interaction.
            if streamStarted == False:
                # Start MJPEG stream. Stop previous stream first if any. Use sudo because stream can be started by another user.
                stdOutAndErr = own_util.runShellCommandWait('sudo killall mjpg_streamer')
                globMyLog.info('going to start lq default stream')
                own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_raspicam.so -vf -hf -fps ' + str(FpsLq) + ' -q 10 -x ' + str(ImgWidth) + ' -y '+ str(ImgHeight) + '" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                # Resolution of PU` Aimetis HD USB Camera Module: 2560x960 or 1280x480. Set contrast (co) and sharpness (sh), range 0..100.
                #own_util.runShellCommandNowait('LD_LIBRARY_PATH=/opt/mjpg-streamer/mjpg-streamer-experimental/ /opt/mjpg-streamer/mjpg-streamer-experimental/mjpg_streamer -i "input_uvc.so -d /dev/video0 -f 20 -r 1280x480 -co 50 -sh 100" -o "output_http.so -p 44445 -w /opt/mjpg-streamer/mjpg-streamer-experimental/www"')
                # Delay to give stream time to start up and camera to stabilize.
                time.sleep(5)
                streamStarted = True

            globMyLog.info('going to call captureAndMotionDetection()')
            # Call captureAndMotionDetection(). This function returns with True when motion is detected
            # and dfrobot_video.avi is created. It returns false when the interactive mode is active.
            motionDetected = captureAndMotionDetection()

            if motionDetected:
                if doPrint:
                    print 'motion detected, going to send motion video to Telegram'
                globMyLog.info('motion detected, going to send motion video to Telegram')
                # Send motion video to Telegram.
                communication.sendTelegramVideo('/home/pi/DFRobotUploads/dfrobot_video.avi', 'Motion detected!')
            else:
                # The interactive mode is active. The current MJPEG stream can be kept running.
                # However we indicate to (re)start a new stream at the next Default run, because
                # the stream can be switched by an interactive user.
                # Stop MJPEG stream. Use sudo because stream can be started by another user.
                streamStarted = False

    except Exception,e:
        globMyLog.info('run_dfrobot exception: ' + str(e))
        raise
