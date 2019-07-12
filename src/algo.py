import numpy as np
from threading import Thread
import time
import src.core as core
import logging
import cv2
from PyQt5.QtCore import QThread, pyqtSignal, pyqtSlot, QMutex


class Hybrid(QThread):
    """
    Hybrid solution for minimal motion cases. Rigid rPPG estimation and simultaneously ROI tracking. In case of motion
    the tracked ROI is used for signal extraction, otherwise the more accurate rigid rPPG
    """
    def __init__(self, frame_rate=20, l1=32, l2=256, hr_band=(40/60., 250/60.), width=500, height=500, patch_size=25):
        QThread.__init__(self)
        self.current_frame = None
        self.buffer = []
        self.mtx = QMutex()

        self.Fs = frame_rate
        self.L1 = l1
        self.L2 = l2
        self.hr_band = hr_band

        # Init down-sampling
        self.patch_size = patch_size
        self.w = width
        self.h = height
        self.rows = int(self.h / self.patch_size)
        self.cols = int(self.w / self.patch_size)
        self.subregs = self.rows * self.cols

        # algo variables/ containers
        self.Id_t = []
        self.zero_row = np.zeros(shape=(1, self.subregs), dtype=np.double)
        self.Pt = np.zeros(shape=(self.L2, self.subregs), dtype=np.double)
        self.Zt = np.zeros(shape=(self.L2, self.subregs), dtype=np.double)
        self.shift_idx = 0
        self.counter = frame_rate*2

        self.logger = logging.getLogger("Hybrid")
        self.__init_logger()
        self.logger.info(f"Number of subregions: {self.subregs}")

    def __init_logger(self):
        # Create handlers
        c_handler = logging.StreamHandler()
        f_handler = logging.FileHandler('run.log', mode='a')
        c_handler.setLevel(logging.DEBUG)
        f_handler.setLevel(logging.DEBUG)

        # Create formatters and add it to handlers
        c_format = logging.Formatter('%(name)s | %(levelname)s | %(message)s')
        f_format = logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s')
        c_handler.setFormatter(c_format)
        f_handler.setFormatter(f_format)

        # Add handlers to the logger
        self.logger.addHandler(c_handler)
        self.logger.addHandler(f_handler)

        self.logger.debug('Logger is initialized!')

    @staticmethod
    def __gaussian_blur(img: np.ndarray) -> np.ndarray:
        """
        Calculates and return gaussian smoothed image

        :param img: the source image to be smoothed
        :return: blurred image
        """
        return cv2.GaussianBlur(img, ksize=(0, 0), sigmaX=5)

    def __downscale_image(self, img: np.ndarray) -> np.ndarray:
        """
        Averages subregions

        :param img: the source image to be downscaled
        :return: the sub-regions in rows with 3 color channel columns (shape: sub-regions x color channels)
        """

        Id = np.empty(shape=(self.rows, self.cols, 3), dtype=np.float)
        for i in range(self.rows):
            for j in range(self.cols):
                Id[i, j, :] = np.mean(img[i * self.patch_size:i * self.patch_size + self.patch_size - 1,
                                          j * self.patch_size:j * self.patch_size + self.patch_size - 1, :], axis=(0, 1))

        Id = np.reshape(Id, (Id.shape[0] * Id.shape[1], 3))

        return Id

    @pyqtSlot(np.ndarray)
    def on_new_frame(self, src: np.ndarray):
        self.logger.debug("Rppg thread got frame signal!")   # TODO: remove this part, only for debugging

        self.mtx.lock()
        self.buffer.append(src.copy())
        self.mtx.unlock()

        self.start()

    def run(self):
        while len(self.buffer) != 0:
            # 1) ---> Down-sample image and append to container list
            self.mtx.lock()
            self.Id_t.append(self.__downscale_image(self.__gaussian_blur(self.buffer[0])))
            self.mtx.unlock()

            # Remove processed frame from buffer
            del self.buffer[0]
            self.logger.info(f"Buffer size: {len(self.buffer)}")

            # 2) ---> Extract the PPG signal
            if len(self.Id_t) == self.L1:
                C = np.array(self.Id_t)
                del self.Id_t[0]

                # Pulse extraction algorithm
                P, Z = core.pos(C)

                if self.shift_idx + self.L1 <= self.L2:                     # L2 length is not reached yet
                    # Fill @param self.Pt with calculated P
                    self.__fill_add(P, Z)
                else:                                                       # In this case the L2 length is fully loaded
                    # Chuck the first part and extend with new data
                    self.__chuck_add(P, Z)

                    # 3) ---> Calculate similarity matrix and combine signals every 2 second
                    if self.counter == self.Fs*2:
                        self.logger.debug("Calculating similarity matrix and combining pulse signals...")
                        self.counter = 0

                        # TODO: implement pruning, similarity matrix and stuff

                    self.counter += 1

    def __chuck_add(self, P, Z):
        n = self.shift_idx - 1
        # delete first row (last frame)
        self.Pt = np.delete(self.Pt, 0, 0)
        self.Zt = np.delete(self.Zt, 0, 0)
        # append zeros for the new frame point
        self.Pt = np.append(self.Pt, self.zero_row, 0)
        self.Zt = np.append(self.Zt, self.zero_row, 0)
        # overlap add result
        self.Pt[n:n + self.L1, :] = self.Pt[n:n + self.L1, :] + P
        self.Zt[n:n + self.L1, :] = self.Zt[n:n + self.L1, :] + Z

    def __fill_add(self, P, Z):
        """
        Fills L2 with calculated pulse segments

        :param P: shape: (pulse_sig, subregions)
        :param Z: shape: (intensity_sig, subregions)
        :return: Fills L2
        """
        self.Pt[self.shift_idx:self.shift_idx + self.L1, :] = self.Pt[self.shift_idx:self.shift_idx + self.L1, :] + P
        self.Zt[self.shift_idx:self.shift_idx + self.L1, :] = self.Zt[self.shift_idx:self.shift_idx + self.L1, :] + Z
        self.shift_idx = self.shift_idx + 1