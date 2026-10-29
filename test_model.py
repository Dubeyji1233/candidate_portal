import cv2
import base64
from headphone_detector import detect_from_base64, is_available

print("Is model available?", is_available())

# Open webcam
cap = cv2.VideoCapture(0)
ret, frame = cap.read()
cap.release()

if ret:
    # Convert to base64 just like the browser does
    _, buffer = cv2.imencode('.jpg', frame)
    frame_b64 = "data:image/jpeg;base64," + base64.b64encode(buffer).decode('utf-8')

    print("Sending to backend function...")
    results = detect_from_base64(frame_b64, conf=0.40)
    print("Results:", results)
else:
    print("Camera not found.")