# -*- coding: utf-8 -*-

# Max-Planck-Gesellschaft zur Förderung der Wissenschaften e.V. (MPG) is
# holder of all proprietary rights on this computer program.
# You can only use this computer program if you have closed
# a license agreement with MPG or you get the right to use the computer
# program from someone who is authorized to grant you that right.
# Any use of the computer program without a valid license is prohibited and
# liable to prosecution.
#
# Copyright©2022 Max-Planck-Gesellschaft zur Förderung
# der Wissenschaften e.V. (MPG). acting on behalf of its Max Planck Institute
# for Intelligent Systems. All rights reserved.
#
# Contact: mica@tue.mpg.de


import os
import re
from abc import ABC
from functools import reduce
from pathlib import Path

import loguru
import numpy as np
import torch
import cv2
import math
from insightface.app import FaceAnalysis
from insightface.app.common import Face
from insightface.utils import face_align
from .detectors import FAN
from loguru import logger
from skimage.io import imread
from torch.utils.data import Dataset
from torchvision import transforms


class BaseDataset(Dataset, ABC):
    def __init__(self, name, config, isEval):
        self.K = config.K
        self.isEval = isEval
        self.actors = []
        self.face_dict = {}
        self.name = name
        self.min_max_K = 0
        self.dataset_root = 'dataset'
        self.total_images = 0
        self.image_folder = 'images'
        self.flame_folder = 'FLAME_parameters'
        self.initialize()

    def initialize(self):
        logger.info(f'[{self.name}] Initialization')
        # 只要是数据集都会有npy文件
        image_list_file = f'dataset/image_paths/{str.upper(self.name)}.npy'
        logger.info(f'[{self.name}] Load cached file list: ' + image_list_file)
        self.face_dict = np.load(image_list_file, allow_pickle=True).item()
        self.actors = list(self.face_dict.keys())
        logger.info(f'[Dataset {self.name}] Total {len(self.actors)} actors loaded!')
        self.set_smallest_k()
        # 为把原始图片裁剪成224*224做准备
        self.app = FaceAnalysis(name='antelopev2', providers=['CUDAExecutionProvider'])
        self.app.prepare(ctx_id=0, det_size=(224, 224))
        self.fan = FAN()

    def set_smallest_k(self):
        self.min_max_K = np.Inf
        max_min_k = -np.Inf
        for key in self.face_dict.keys():
            length = len(self.face_dict[key][0])
            if length < self.min_max_K:
                self.min_max_K = length
            if length > max_min_k:
                max_min_k = length

        self.total_images = reduce(lambda k, l: l + k, map(lambda e: len(self.face_dict[e][0]), self.actors))
        loguru.logger.info(f'Dataset {self.name} with min K = {self.min_max_K} max K = {max_min_k} length = {len(self.face_dict)} total images = {self.total_images}')
        return self.min_max_K

    def __len__(self):
        return len(self.actors)

    def __getitem__(self, index):
        actor = self.actors[index]
        images, params_path = self.face_dict[actor]
        # 把actor前缀消除
        images = [path.split('/')[1] for path in images]
        images = [Path(self.dataset_root, self.name, self.image_folder, path) for path in images]
        sample_list = np.array(np.random.choice(range(len(images)), size=self.K, replace=False))

        K = self.K
        if self.isEval:
            K = max(0, min(200, self.min_max_K))
            sample_list = np.array(range(len(images))[:K])

        params = np.load(os.path.join(self.dataset_root, self.name, self.flame_folder, params_path), allow_pickle=True)
        pose = torch.tensor(params['pose']).float()
        betas = torch.tensor(params['betas']).float()

        flame = {
            'shape_params': torch.cat(K * [betas[:300][None]], dim=0),
            'expression_params': torch.cat(K * [betas[300:][None]], dim=0),
            'pose_params': torch.cat(K * [torch.cat([pose[:3], pose[6:9]])[None]], dim=0),
        }

        arcface_list = []
        landmark_list = []

        for i in sample_list:
            image_path = images[i]
            img = cv2.imread(str(image_path))
            # 以下部分是在跑一个人脸检测模型，得到置信分数最高的边界框，然后裁剪
            bboxes, kpss = self.app.det_model.detect(img, max_num=0, metric='default')
            if bboxes.shape[0] == 0:
                continue
            i = get_center(bboxes, img)
            bbox = bboxes[i, 0:4]
            det_score = bboxes[i, 4]
            kps = None
            if kpss is not None:
                kps = kpss[i]

            face = Face(bbox=bbox, kps=kps, det_score=det_score)
            arcface = face_align.norm_crop(img, landmark=face.kps, image_size=224)
            arcface = arcface / 255.0
            arcface_list.append(arcface)

            # 获得 landmarks
            landmark = self.fan.model.get_landmarks(img)
            landmark_list.append(landmark[0])

        images_array = torch.from_numpy(np.array(arcface_list)).float()
        landmarks = torch.from_numpy(np.array(landmark_list)).float()

        return {
            'images': images_array,
            'imagename': actor,
            'dataset': self.name,
            'flame': flame,
            'landmark':landmarks
            # 'mask':masks
        }


def dist(p1, p2):
    return math.sqrt(((p1[0] - p2[0]) ** 2) + ((p1[1] - p2[1]) ** 2))


def get_center(bboxes, img):
    img_center = img.shape[0] // 2, img.shape[1] // 2
    size = bboxes.shape[0]
    distance = np.Inf
    j = 0
    for i in range(size):
        x1, y1, x2, y2 = bboxes[i, 0:4]
        dx = abs(x2 - x1) / 2.0
        dy = abs(y2 - y1) / 2.0
        current = dist((x1 + dx, y1 + dy), img_center)
        if current < distance:
            distance = current
            j = i

    return j