#!/usr/bin/env python3
# coding: utf-8
"""
Implementation of Capsule Networks:
"""
import os
import sys
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from time import time
from datetime import datetime
from PIL import Image
import argparse
from collections import defaultdict

from keras import layers, models, optimizers
from keras import backend as K

K.set_image_data_format('channels_last')
from keras.utils import to_categorical
from keras.preprocessing.image import ImageDataGenerator
from keras import callbacks

from keras.layers import Dense, Flatten
from keras.layers import Conv2D, MaxPooling2D, Dropout
from keras.models import Sequential
from keras.losses import categorical_crossentropy

from utils import plot_log, save_results_to_csv
from capsulelayers import CapsuleLayer, PrimaryCap, Length, Mask

sys.path.append('./PatchyTools/')
from PatchyConverter import PatchyConverter
from DropboxLoader import DropboxLoader
from CapsuleParameters import CapsuleParameters
from CapsuleParameters import CapsuleTrainingParameters

# from ConvNetPatchy import AccuracyHistory

DIR_PATH = os.environ['GAMMA_DATA_ROOT']
GRAPH_RELABEL_NAME = '_relabelled'
RESULTS_PATH = os.path.join(DIR_PATH, 'Results/CapsuleSans/CNN_Caps_comparison.csv')


class GraphClassifier(object):
    def __init__(self, input_shape, n_class=2, routings=3):
        # Fixed initialization parameters:
        self.input_shape = input_shape
        self.n_class = n_class
        self.routings = routings

    def import_data(self, data):
        (self.x_train, self.y_train), (self.x_test, self.y_test) = data

        # assert(self.input_shape == x_train.shape[1:], 'input shape doesnt match ')

        self.y_train = pd.get_dummies(self.y_train).values
        self.y_test = pd.get_dummies(self.y_test).values

    def import_nn_parameters(self, params):
        self.conv_layer = params.get_layer_params('conv_layer')
        self.primary_caps_layer = params.get_layer_params('caps_layer')
        self.digit_caps_layer = params.get_layer_params('digitcaps_layer')
        self.decoder_layer = params.get_layer_params('decoder_layer')

    def build_cnn_graph(self):

        self.cnn_model = Sequential()
        self.cnn_model.add(Conv2D(16, kernel_size=(5, 5), strides=(1, 1), activation='relu', input_shape=input_shape,
                                  kernel_initializer='glorot_uniform'))
        # model.add(MaxPooling2D(pool_size=(2, 2), strides=(2, 2)))
        self.cnn_model.add(Conv2D(8, kernel_size=(5, 5), activation='relu', kernel_initializer='glorot_uniform'))
        # model.add(MaxPooling2D(pool_size=(2, 2)))
        self.cnn_model.add(Flatten())
        self.cnn_model.add(Dense(128, activation='relu', kernel_initializer='glorot_uniform'))
        self.cnn_model.add(Dropout(0.5))
        self.cnn_model.add(Dense(self.n_class, activation='softmax'))

        self.cnn_model.compile(loss=categorical_crossentropy,
                               optimizer=optimizers.Adam(),
                               metrics=['accuracy'])

        # train_model = models.Model([x, y], [out_caps, decoder(masked_by_y)])
        # eval_model = models.Model(x, [out_caps, decoder(masked)])
        #
        # return train_model,eval_model,_

    def build_the_graph(self, params):
        """
        A Capsule Network on MNIST.
        :param input_shape: data shape, 3d, [width, height, channels]
        :param n_class: number of classes
        :param routings: number of routing iterations
        :return: Two Keras Models, the first one used for training, and the second one for evaluation.
                `eval_model` can also be used for training.
        """

        self.import_nn_parameters(params)

        start = time()
        x = layers.Input(shape=self.input_shape)

        # Layer 1: Just a conventional Conv2D layer
        # params_conv_layer = self.params[0]

        conv1 = layers.Conv2D(filters=self.conv_layer['filters'],
                              kernel_size=self.conv_layer['kernel_size'],
                              strides=self.conv_layer['strides'],
                              padding=self.conv_layer['padding'],
                              activation=self.conv_layer['activation'],
                              name=self.conv_layer['activation'])(x)
        # filters=128,
        # kernel_size=9,
        # strides=1,
        # padding='valid',
        # activation='relu',
        # name='conv1')(x)

        # Layer 2: Conv2D layer with `squash` activation, then reshape to [None, num_capsule, dim_capsule]
        primarycaps = PrimaryCap(conv1,
                                 dim_capsule=self.primary_caps_layer['dim_capsule'],
                                 n_channels=self.primary_caps_layer['n_channels'],
                                 kernel_size=self.primary_caps_layer['kernel_size'],
                                 strides=self.primary_caps_layer['strides'],
                                 padding=self.primary_caps_layer['padding'])
        # dim_capsule=8,
        # n_channels=32,
        # kernel_size=2,
        # strides=2,
        # padding='valid')

        # Layer 3: Capsule layer. Routing algorithm works here.
        digitcaps = CapsuleLayer(num_capsule=self.n_class,
                                 dim_capsule=self.digit_caps_layer['dim_capsule'],
                                 # /dim_capsule = 16
                                 routings=self.routings,
                                 name=self.digit_caps_layer['name'])(primarycaps)

        # Layer 4: This is an auxiliary layer to replace each capsule with its length. Just to match the true label's shape.
        # If using tensorflow, this will not be necessary. :)
        out_caps = Length(name='capsnet')(digitcaps)

        # Decoder network.
        y = layers.Input(shape=(self.n_class,))
        masked_by_y = Mask()([digitcaps, y])  # The true label is used to mask the output of capsule layer. For training
        masked = Mask()(digitcaps)  # Mask using the capsule with maximal length. For prediction

        # Shared Decoder model in training and prediction
        decoder = models.Sequential(name='decoder')
        decoder.add(layers.Dense(self.decoder_layer['first_dense'], activation='relu',
                                 input_dim=self.digit_caps_layer['dim_capsule'] * self.n_class))
        decoder.add(layers.Dense(self.decoder_layer['second_dense'], activation='relu'))
        # decoder.add(layers.Dropout(0.5))
        # decoder.add(layers.Dense(128, activation='relu', input_dim=16 * self.n_class))
        # decoder.add(layers.Dense(256, activation='relu'))
        # decoder.add(layers.Dense(np.prod(self.input_shape), activation='sigmoid'))
        decoder.add(layers.Dense(np.prod(self.input_shape), activation='softmax'))
        decoder.add(layers.Reshape(target_shape=self.input_shape, name='out_recon'))

        # Models for training and evaluation (prediction)
        train_model = models.Model([x, y], [out_caps, decoder(masked_by_y)])
        eval_model = models.Model(x, [out_caps, decoder(masked)])

        # manipulate model
        noise = layers.Input(shape=(self.n_class, self.digit_caps_layer['dim_capsule']))  # 16
        noised_digitcaps = layers.Add()([digitcaps, noise])
        masked_noised_y = Mask()([noised_digitcaps, y])
        manipulate_model = models.Model([x, y, noise], decoder(masked_noised_y))
        self.train_model = train_model
        self.eval_model = eval_model
        self.manipulate_model = manipulate_model
        print('time to generate the model: {}'.format(time() - start))
        return train_model, eval_model, manipulate_model

    def margin_loss(self, y_true, y_pred):
        """
        Margin loss for Eq.(4). When y_true[i, :] contains not just one `1`, this loss should work too. Not test it.
        :param y_true: [None, n_classes]
        :param y_pred: [None, num_capsule]
        :return: a scalar loss value.
        """
        L = y_true * K.square(K.maximum(0., 0.9 - y_pred)) + \
            0.5 * (1 - y_true) * K.square(K.maximum(0., y_pred - 0.1))

        return K.mean(K.sum(L, 1))

    def train_generator(self, x, y, batch_size, shift_fraction=0.1):
        train_datagen = ImageDataGenerator(width_shift_range=shift_fraction,
                                           height_shift_range=shift_fraction)  # shift up to 2 pixel for MNIST
        generator = train_datagen.flow(x, y, batch_size=batch_size)
        while 1:
            x_batch, y_batch = generator.next()
            yield ([x_batch, y_batch], [y_batch, x_batch])

    def train(self, data, args):
        """
        Training a CapsuleNet
        :param model: the CapsuleNet model
        :param data: a tuple containing training and testing data, like `((x_train, y_train), (x_test, y_test))`
        :param args: arguments
        :return: The trained model
        """
        self.import_data(data)
        # self.history = AccuracyHistory()

        # if not hasattr(self, 'train_model'):
        #     self.build_the_graph()
        # time:
        start = time()
        # callbacks
        # self.log_file = args.save_dir + '/log.csv'
        self.log_file = os.path.join(args.save_dir, args.log_filename)
        # self.log_file = args.save_dir + '/'+ args.log_filename

        log = callbacks.CSVLogger(self.log_file)
        tb = callbacks.TensorBoard(log_dir=args.save_dir + '/tensorboard-logs',
                                   batch_size=args.batch_size, histogram_freq=int(args.debug))
        checkpoint = callbacks.ModelCheckpoint(args.save_dir + '/weights-{epoch:02d}.h5', monitor='val_capsnet_acc',
                                               save_best_only=True, save_weights_only=True, verbose=0)
        lr_decay = callbacks.LearningRateScheduler(schedule=lambda epoch: args.lr * (args.lr_decay ** epoch))

        # compile the model

        self.train_model.compile(optimizer=optimizers.Adam(lr=args.lr),
                                 loss=[self.margin_loss, 'mse'],
                                 loss_weights=[1., args.lam_recon],
                                 metrics={'capsnet': 'accuracy'})

        # Training without data augmentation:
        # print('shape validation : ', np.array([[self.x_test, self.y_test], [self.y_test, self.x_test]]).shape)
        if args.data_augmentation == False:
            self.train_model.fit([self.x_train, self.y_train], [self.y_train, self.x_train],
                                 batch_size=args.batch_size,
                                 epochs=args.epochs,
                                 validation_data=[[self.x_test, self.y_test], [self.y_test, self.x_test]],
                                 # validation_data=[self.x_test, self.y_test], #[self.y_test, self.x_test]],
                                 # callbacks=[log, tb, checkpoint, lr_decay,TQDMCallback()],
                                 callbacks=[log, tb, checkpoint, lr_decay],
                                 verbose=args.verbose)
            # print('Evaluation: ',self.train_model.predict([[self.x_test, self.y_test], [self.y_test, self.x_test]]))
        else:
            # Begin: Training with data augmentation ---------------------------------------------------------------------#
            # Training with data augmentation. If shift_fraction=0., also no augmentation.
            self.train_model.fit_generator(
                generator=self.train_generator(self.x_train, self.y_train, args.batch_size, args.shift_fraction),
                steps_per_epoch=int(y_train.shape[0] / args.batch_size),
                epochs=args.epochs,
                validation_data=[[self.x_test, self.y_test], [self.y_test, self.x_test]],
                callbacks=[log, tb, checkpoint, lr_decay])
            # End: Training with data augmentation -----------------------------------------------------------------------#
            self.train_model.save_weights(args.save_dir + '/trained_model.h5')
        print('Trained model saved to \'%s/trained_model.h5\'' % args.save_dir)

        # Save the results:
        if args.plot_log == True:
            plot_log(self.log_file, show=True)

        self.training_time = time() - start

        self.get_accuracy_results(args)

    def get_accuracy_results(self, args):  # , index): # show=True):
        df = pd.read_csv(self.log_file)  # ,index_col=0)
        df = df.loc[:, ['epoch', 'capsnet_acc', 'val_capsnet_acc']]
        results = df.iloc[-1, :]  # .val_capsnet_acc

        # Adding other variables:
        results.epoch = results.epoch + 1
        results.rename(None, inplace=True)

        results = results.append(pd.Series({'time': self.training_time}))
        results = results.append(pd.Series({'lam_recon': args.lam_recon}))
        results = results.append(pd.Series({'lr': args.lr}))
        results = results.append(pd.Series({'lr_decay': args.lr_decay}))
        results = results.append(pd.Series({'routing': args.routing}))
        results = results.append(pd.Series({'fold': args.fold}))

        self.results = results


if __name__ == "__main__":

    # Arguments:
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', help='name_of the dataset', default='MUTAG')
    parser.add_argument('-k', help='receptive field for patchy', default=10)
    parser.add_argument('-r', dest='relabelling', help='reshuffling takes place', action='store_true')
    parser.add_argument('-nr', dest='relabelling', help='no reshuffling takes place', action='store_false')
    parser.set_defaults(relabelling=True)

    # parser.add_argument('-sampling_ratio', help='ratio to sample on', default=0.2)

    # Parsing arguments:
    args = parser.parse_args()

    # Arguments:
    dataset_name = args.n
    # width = int(args.w)
    receptive_field = int(args.k)
    relabelling = args.relabelling


    # print('relabelling:')
    # print('')
    # print(relabelling)

    # dataset_name = 'MUTAG'
    # width = 18
    # receptive_field = 10

    # Converting Graphs into Matrices:
    graph_converter = PatchyConverter(dataset_name, receptive_field)
    
    print('Graph imported')
    if relabelling:
        print('Relabelling:')
        graph_converter.relabel_graphs()

    graph_tensor = graph_converter.graphs_to_Patchy_tensor()
    avg_nodes_per_graph = graph_converter.avg_nodes_per_graph
