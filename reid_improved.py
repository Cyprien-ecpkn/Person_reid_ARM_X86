#!/usr/bin/python3
# -*- coding: utf-8 -*-
# @Time : 2021/1/18 下午6:02
# @Author : zengwb

import os
import cv2
import torch
import warnings
import argparse
import numpy as np
import onnxruntime as ort
from utils.datasets import LoadStreams, LoadImages, LoadWebcam
from utils.draw import draw_boxes
from utils.general import check_img_size
from utils.torch_utils import time_synchronized
from person_detect_yolov5 import Person_detect
from deep_sort import build_tracker
from utils.parser import get_config
from utils.log import get_logger
from utils.torch_utils import select_device, load_classifier, time_synchronized
# count
from collections import Counter
from collections import deque
import math
from PIL import Image, ImageDraw, ImageFont


def tlbr_midpoint(box):
    minX, minY, maxX, maxY = box
    midpoint = (int((minX + maxX) / 2), int((minY + maxY) / 2))  # minus y coordinates to get proper xy format
    return midpoint


def intersect(A, B, C, D):
    return ccw(A, C, D) != ccw(B, C, D) and ccw(A, B, C) != ccw(A, B, D)


def ccw(A, B, C):
    return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])


def vector_angle(midpoint, previous_midpoint):
    x = midpoint[0] - previous_midpoint[0]
    y = midpoint[1] - previous_midpoint[1]
    return math.degrees(math.atan2(y, x))


def get_size_with_pil(label,size=25):
    font = ImageFont.truetype("./configs/simkai.ttf", size, encoding="utf-8")  # simhei.ttf
    return font.getsize(label)


#为了支持中文，用pil
def put_text_to_cv2_img_with_pil(cv2_img,label,pt,color):
    pil_img = cv2.cvtColor(cv2_img, cv2.COLOR_BGR2RGB)  # cv2和PIL中颜色的hex码的储存顺序不同，需转RGB模式
    pilimg = Image.fromarray(pil_img)  # Image.fromarray()将数组类型转成图片格式，与np.array()相反
    draw = ImageDraw.Draw(pilimg)  # PIL图片上打印汉字
    font = ImageFont.truetype("./configs/simkai.ttf", 25, encoding="utf-8") #simhei.ttf
    draw.text(pt, label, color,font=font)
    return cv2.cvtColor(np.array(pilimg), cv2.COLOR_RGB2BGR)  # 将图片转成cv2.imshow()可以显示的数组格式


colors = np.array([
    [1,0,1],
    [0,0,1],
    [0,1,1],
    [0,1,0],
    [1,1,0],
    [1,0,0]
    ]);

def get_color(c, x, max):
    ratio = (x / max) * 5;
    i = math.floor(ratio);
    j = math.ceil(ratio);
    ratio -= i;
    r = (1 - ratio) * colors[i][c] + ratio * colors[j][c];
    return r;

def compute_color_for_labels(class_id,class_total=80):
    offset = (class_id + 0) * 123457 % class_total;
    red = get_color(2, offset, class_total);
    green = get_color(1, offset, class_total);
    blue = get_color(0, offset, class_total);
    return (int(red*256),int(green*256),int(blue*256))


class yolo_reid():
    def __init__(self, cfg, args, path):
        self.logger = get_logger("root")
        self.args = args
        self.video_path = path
        use_cuda = args.use_cuda and torch.cuda.is_available()
        if not use_cuda:
            warnings.warn("Running in cpu mode which maybe very slow!", UserWarning)

        self.person_detect = Person_detect(self.args, self.video_path)
        imgsz = check_img_size(args.img_size, s=32)  # self.model.stride.max())  # check img_size # check img_size
        if args.cam == -1 and not args.ipcam:
            self.dataset = LoadImages(self.video_path, img_size=imgsz)
            self.wbc = -1
        elif args.ipcam :
            self.wbc = 1
            self.dataset = LoadWebcam(self.video_path, img_size=imgsz)
        else:
            # This part is not tested. 
            self.dataset = LoadStreams(args.cam, img_size=imgsz)
            self.wbc = 0
        print('webcam : ' ,self.wbc)
        self.deepsort = build_tracker(cfg, args.sort, use_cuda=use_cuda)
        self.img_cnt = 0

        # special for the class test
        self.new_ID_Threshold = args.id_thres
        self.real_ID_list = []      # the final ID list in the class
        self.pre_ID_location_list = []      # the ID's location

        self.life_len = 10
        self.exit_ID_life = []      # if an ID disappear life_len films or more, The

        self.total_out = []

    def Threshold_test(self, bbox, id_bbox):
        # is the new id return 1 else return 0
        x1, y1, x2, y2 = bbox
        id_x1, id_y1, id_x2, id_y2 = id_bbox
        stander_x = (id_x2 - id_x1) * self.new_ID_Threshold
        stander_y = (id_y2 - id_y1) * self.new_ID_Threshold
        if x1 < id_x1 - stander_x or x2 > id_x2 + stander_x or y1 < id_y1 - stander_y or y2 > id_y2 - stander_y:
            return 1
        else:
            return 0

    # Add two new function to make sure ID information continuously
    def new_ID_judge(self, bbox):
        # return the bbox's id in the real_ID_list's location
        if len(self.pre_ID_location_list) == 0:
            return -1
        else:
            cnt = 0
            for id_box in self.pre_ID_location_list:
                result = self.Threshold_test(bbox, id_box)
                if result == 0:
                    return cnt
                cnt += 1
            return -1

    def kill_ID(self):
        remove_list = []
        for i in range(len(self.exit_ID_life)):
            self.exit_ID_life[i] -= 1
            if self.exit_ID_life[i] == 0:
                remove_list.append(i)

        if len(remove_list) > 0:
            remove_list.reverse()
            for k_id in remove_list:
                self.real_ID_list.pop(k_id)
                self.pre_ID_location_list.pop(k_id)
                self.exit_ID_life.pop(k_id)



    def deep_sort(self):
        idx_frame = 0
        temp_path, vid_writer = None, None
        fourcc='mp4v'
        if not self.args.img : 
            save_path = './output/'+self.args.outname+'.mp4'
        else: 
            save_path = './output/' + self.args.outname + '.jpg'
        for video_path, img, ori_img, vid_cap in self.dataset:
            idx_frame += 1

            
            if self.wbc == 0:
                ori_img = np.array(ori_img)
                img = img[0]
                ori_img = ori_img[0]
            # print('aaaaaaaa', video_path, img.shape, im0s.shape, vid_cap)
            t1 = time_synchronized()

            # yolo detection
            bbox_xywh, cls_conf, cls_ids, xy = self.person_detect.detect(video_path, img, ori_img, vid_cap)

            # do tracking
            outputs = self.deepsort.update(bbox_xywh, cls_conf, ori_img)

            # The outputs is a list object
            # The outputs[0] is like this [x1 y1 x2 y2 ID] and the x1 y1 x2 y2 is the boxes location
            # print(len(outputs))
            # if len(outputs) > 0:
            #     print("The output shape is : ")
            #     print(outputs[0].shape)
            #     print(outputs)

            # 1. update ID information
            track_cnt = 0
            for track in outputs:
                track_id = track[-1]
                bbox = track[:4]

                id_loc = self.new_ID_judge(bbox)
                if id_loc == -1:
                    self.real_ID_list.append(track_id)
                    self.pre_ID_location_list.append(bbox)
                    self.exit_ID_life.append(self.life_len)
                else:
                    outputs[track_cnt][-1] = self.real_ID_list[id_loc]
                    self.pre_ID_location_list[id_loc] = bbox
                    self.exit_ID_life[id_loc] = self.life_len

                track_cnt += 1

            # make sure ID continuously
            self.kill_ID()
            self.total_out.append(outputs)

            # 2. draw
            if len(outputs) > 0:
                bbox_tlwh = []
                bbox_xyxy = outputs[:, :4]
                identities = outputs[:, -1]
                ori_img = draw_boxes(ori_img, bbox_xyxy, identities)

                for bb_xyxy in bbox_xyxy:
                    bbox_tlwh.append(self.deepsort._xyxy_to_tlwh(bb_xyxy))

                # results.append((idx_frame - 1, bbox_tlwh, identities))
            print("yolo+deepsort:", time_synchronized() - t1)

            end = time_synchronized()

            if self.args.display:
                # cv2.imshow("test", ori_img)
                cv2.imwrite("./json_img/{}.jpg".format(self.img_cnt), ori_img)
                print(self.img_cnt)
                self.img_cnt += 1
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
            if self.args.img: 
                cv2.imwrite(save_path,ori_img)
            else:
                if temp_path != save_path:  # new video
                    temp_path = save_path
                    if isinstance(vid_writer, cv2.VideoWriter):
                        vid_writer.release()  # release previous video writer
        
                    fps = vid_cap.get(cv2.CAP_PROP_FPS)
                    width = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    vid_writer = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*fourcc), fps, (width, height))
                vid_writer.write(ori_img)
            
            self.logger.info("{}/time: {:.03f}s, fps: {:.03f}, detection numbers: {}, tracking numbers: {}" \
                             .format(idx_frame, end - t1, 1 / (end - t1),
                                     bbox_xywh.shape[0], len(outputs)))




def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_path", dest='video_path',default='./student_demo.mp4', type=str)
    parser.add_argument("--camera", action="store", dest="cam", type=int, default="-1")
    parser.add_argument('--device', default='cuda:0', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--img', action='store_true', default = False, help='whether input is a image (.jpg)')
    parser.add_argument('--ipcam', action='store_true', default = False, help='whether input is a ip webcam (.jpg)')
    parser.add_argument('--id_thres',type=float,default=0.3,help='Threshold to distinguish different persons')
    # yolov5
    parser.add_argument('--weights', nargs='+', type=str, default='./weights/yolov5s.pt', help='model.pt path(s)')
    parser.add_argument('--img-size', type=int, default=960, help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float, default=0.4, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.5, help='IOU threshold for NMS')
    parser.add_argument('--classes', default=[0], type=int, help='filter by class: --class 0, or --class 0 2 3')
    parser.add_argument('--agnostic-nms', action='store_true', help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')

    # deep_sort
    parser.add_argument("--sort", default=True, help='True: sort model, False: reid model')
    parser.add_argument("--config_deepsort", type=str, default="./configs/deep_sort.yaml")
    parser.add_argument("--display", default=True, help='show result')
    parser.add_argument("--frame_interval", type=int, default=1)
    parser.add_argument("--cpu", dest="use_cuda", action="store_false", default=True)

    parser.add_argument("--outname", default='output_improved', type=str)

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    cfg = get_config()
    cfg.merge_from_file(args.config_deepsort)

    yolo_reid = yolo_reid(cfg, args, path=args.video_path)
    with torch.no_grad():
        yolo_reid.deep_sort()
