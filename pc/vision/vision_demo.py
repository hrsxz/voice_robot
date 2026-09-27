from pc.constants import project_root_path
from pc.vision.calibration import MapCalibrator
from pc.vision.camera_stream import CameraStream
from pc.vision.data_types import Point
from pc.vision.vision_system import VisionSystem

camera = CameraStream(camera_index=0)
frame = camera.read()

calibrator = MapCalibrator(project_root_path / "pc" / "vision" / "map_config.yaml")
result = calibrator.calibrate(frame)

print(result)

if result["status"] != "ok":
    raise RuntimeError(result["detail"])

# Extract the pixel coordinates of the center point of the frame
height, width = frame.shape[:2]
pixel_point = Point(width / 2, height / 2)
map_point = calibrator.pixel_to_map(pixel_point)

print(map_point)


vision = VisionSystem(camera_index=0, marker_id=0)
state = vision.get_world_state()
