import os
import datetime
import numpy as np
import livelossplot
import tensorflow as tf
from abc import ABC, abstractmethod
from plot_keras_history import plot_history
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.optimizers.schedules import PiecewiseConstantDecay
from tensorflow.keras.callbacks import EarlyStopping
from tensorflow.keras.utils import Progbar
from matplotlib.pyplot import show
import horovod.tensorflow.keras as hvd

from .utils import (Timing, list_devices, set_gpu_memory_growth, 
                    set_visible_gpus, checkarg_model, MODELS)
from .dataloader import DataGenerator, create_batch_hr_lr
from .losses import dssim, dssim_mae, dssim_mae_mse, dssim_mse
from .resnet_bi import resnet_bi, recurrent_resnet_bi
from .resnet_rc import resnet_rc, recurrent_resnet_rc
from .resnet_spc import resnet_spc, recurrent_resnet_spc
from .cgan import train_step
from .discriminator import residual_discriminator


class Trainer(ABC):
    """        
    """
    def __init__(
        self,
        model, 
        loss='mae',
        batch_size=64, 
        device='GPU', 
        gpu_memory_growth=True,
        use_multiprocessing=False,
        verbose=True, 
        model_list=None
        ):
        """
        """
        self.model = model
        self.batch_size = batch_size
        self.loss = loss
        self.device = device
        self.gpu_memory_growth = gpu_memory_growth
        self.use_multiprocessing = use_multiprocessing
        self.verbose = verbose
        self.timing = Timing()
       
        ### Initializing Horovod
        hvd.init()

        ### Setting up devices
        if self.verbose in [1 ,2]:
            print('List of devices:')
        if self.device == 'GPU':
            if self.gpu_memory_growth:
                set_gpu_memory_growth(verbose=False)
            # pin GPU to be used to process local rank (one GPU per process)       
            set_visible_gpus(hvd.local_rank())
            devices = list_devices('physical', gpu=True, verbose=verbose) 
        elif device == 'CPU':
            devices = list_devices('physical', gpu=False, verbose=verbose)
        else:
            raise ValueError('device not recognized')

        n_devices = len(devices)
        if self.verbose in [1 ,2]:
            print ('Number of devices: {}'.format(n_devices))
        batch_size_per_replica = self.batch_size
        self.global_batch_size = batch_size_per_replica * n_devices
        if self.verbose in [1 ,2]:
            print(f'Global batch size: {self.global_batch_size}, per replica: {batch_size_per_replica}')

        # identifying the first Horovod worker (for distributed training with GPUs), or CPU training
        if (self.device == 'GPU' and hvd.rank() == 0) or self.device == 'CPU':
            self.running_on_first_worker = True
        else:
            self.running_on_first_worker = False

        ### Checking the model argument
        if model_list is None:
            model_list = MODELS
        self.model = checkarg_model(self.model, model_list)

        ### Choosing the loss function
        if loss == 'mae':  # L1 pixel loss
            self.lossf = tf.keras.losses.MeanAbsoluteError()
        elif loss == 'mse':  # L2 pixel loss
            self.lossf = tf.keras.losses.MeanSquaredError()
        elif loss == 'dssim':
            self.lossf = dssim
        elif loss == 'dssim_mae':
            self.lossf = dssim_mae
        elif loss == 'dssim_mse':
            self.lossf = dssim_mse
        elif loss == 'dssim_mae_mse':
            self.lossf = dssim_mae_mse
        else:
            raise ValueError('`loss` not recognized')

    @abstractmethod
    def run(self):
        pass
        

class SupervisedTrainer(Trainer):
    """Procedure for training the supervised residual models
    """
    def __init__(
        self,
        model, 
        data_train, 
        data_val, 
        data_test,  
        predictors_train=None,
        predictors_val=None,
        predictors_test=None,
        loss='mae',
        batch_size=64, 
        device='GPU', 
        gpu_memory_growth=True,
        use_multiprocessing=False, 
        model_list=None,
        topography=None, 
        landocean=None,
        scale=5, 
        interpolation='bicubic', 
        patch_size=50, 
        time_window=None,
        epochs=60, 
        steps_per_epoch=None, 
        validation_steps=None, 
        test_steps=None,
        learning_rate=1e-4, 
        lr_decay_after=1e5,
        early_stopping=False, 
        patience=6, 
        min_delta=0, 
        plot='plt', 
        show_plot=True, 
        save_plot=False,
        save=False,
        save_path=None, 
        savecheckpoint_path='./checkpoints/',
        verbose=True,
        **architecture_params
        ):
        """Procedure for training the supervised residual models

        Parameters
        ----------
        model : str
            String with the name of the model architecture, either 'resnet_spc', 
            'resnet_bi' or 'resnet_rc'.
        data_train : 4D ndarray
            Training dataset with dims [nsamples, lat, lon, 1]. This grids must 
            correspond to the observational reference at HR, from which a 
            coarsened version will be created to produce paired samples. 
        data_val : 4D ndarray
            Validation dataset with dims [nsamples, lat, lon, 1]. This holdout 
            dataset is used at the end of each epoch to check the losses and 
            diagnose overfitting.
        data_test : 4D ndarray
            Testing dataset with dims [nsamples, lat, lon, 1]. Holdout not used
            during training. 
        predictors_train : tuple of 4D ndarray, optional
            Predictor variables for trianing. Given as tuple of 4D ndarray with 
            dims [nsamples, lat, lon, 1]. 
        predictors_val : tuple of 4D ndarray, optional
            Predictor variables for validation. Given as tuple of 4D ndarray 
            with dims [nsamples, lat, lon, 1]. 
        predictors_test : tuple of 4D ndarray, optional
            Predictor variables for testing. Given as tuple of 4D ndarray with 
            dims [nsamples, lat, lon, 1]. 
        topography : None or 2D ndarray, optional
            Elevation data.
        landocean : None or 2D ndarray, optional
            Binary land-ocean mask.
        scale : int, optional
            Scaling factor. 
        interpolation : str, optional
            Interpolation used when upsampling/downsampling the training samples.
            By default 'bicubic'. 
        patch_size : int or None, optional
            Size of the square patches used to grab training samples.
        time_window : int or None, optional
            If not None, then each sample will have a temporal dimension 
            (``time_window`` slices to the past are grabbed for the LR array).
        batch_size : int, optional
            Batch size per replica.
        epochs : int, optional
            Number of epochs or passes through the whole training dataset. 
        steps_per_epoch : int or None, optional
            Total number of steps (batches of samples) before decalrin one epoch
            finished.``batch_size * steps_per_epoch`` samples are passed per 
            epoch. If None, ``then steps_per_epoch`` is equal to the number of 
            samples diviced by the ``batch_size``.
        validation_steps : int, optional
            Steps using at the end of each epoch for drawing validation samples. 
        test_steps : int, optional
            Steps using after training for drawing testing samples.
        learning_rate : float or tuple of floats, optional
            Learning rate. If a tuple is given, it corresponds to the min and max
            LR used for a PiecewiseConstantDecay scheduler.
        lr_decay_after : float or None, optional
            Used for the PiecewiseConstantDecay scheduler.
        early_stopping : bool, optional
            Whether to use early stopping.
        patience : int, optional
            Patience for early stopping. 
        min_delta : float, otional 
            Min delta for early stopping.
        save : bool, optional
            Whether to save the final model. 
        save_path : None or str
            Path for saving the final model. If None, then ``'./saved_model/'`` 
            is used. The SavedModel format is a directory containing a protobuf 
            binary and a TensorFlow checkpoint.
        savecheckpoint_path : None or str
            Path for saving the training checkpoints. If None, then no 
            checkpoints are saved during training. 
        device : str
            Choice of 'GPU' or 'CPU' for the training of the Tensorflow models. 
        gpu_memory_growth : bool, optional
            By default, TensorFlow maps nearly all of the GPU memory of all GPUs.
            If True, we request to only grow the memory usage as is needed by 
            the process.
        plot : str, optional
            Either 'plt' for static plot of the learning curves or 'llp' for 
            interactive plotting (useful on jupyterlab as an alternative to 
            Tensorboard).
        show_plot : bool, optional
            If True the static plot is shown after training. 
        save_plot : bool, optional
            If True the static plot is saved to disk after training. 
        verbose : bool, optional
            Verbosity mode. False or 0 = silent. True or 1, max amount of 
            information is printed out. When equal 2, then less info is shown.
        **architecture_params : dict
            Dictionary with additional parameters passed to the neural network 
            model.
        """
        super().__init__(model, loss, batch_size, device, gpu_memory_growth,
                         use_multiprocessing, verbose, model_list)
        self.data_train = data_train
        self.data_val = data_val
        self.data_test = data_test
        self.predictors_train = predictors_train
        self.predictors_val = predictors_val
        self.predictors_test = predictors_test
        self.topography = topography 
        self.landocean = landocean
        self.scale = scale
        self.interpolation = interpolation 
        self.patch_size = patch_size 
        self.time_window = time_window
        self.epochs = epochs
        self.steps_per_epoch = steps_per_epoch
        self.validation_steps = validation_steps
        self.test_steps = test_steps
        self.learning_rate = learning_rate
        self.lr_decay_after = lr_decay_after
        self.early_stopping = early_stopping
        self.patience = patience
        self.min_delta = min_delta
        self.save = save
        self.save_path = save_path
        self.savecheckpoint_path = savecheckpoint_path
        self.plot = plot
        self.show_plot = show_plot
        self.save_plot = save_plot
        self.architecture_params = architecture_params

        self.setup_datagen()
        self.setup_model()
        self.run()

    def setup_datagen(self):
        """Setting up the data generators
        """
        if self.patch_size is not None and self.patch_size % self.scale != 0:
            raise ValueError('`patch_size` must be divisible by `scale` (remainder must be zero)')

        recmodels = ['recurrent_resnet_spc', 'recurrent_resnet_rc', 'recurrent_resnet_bi']
        if self.time_window is not None and self.model not in recmodels:
            msg = f'``time_window={self.time_window}``, choose a model that handles samples with a temporal dimension'
            raise ValueError(msg)
        if self.model in recmodels and self.time_window is None:
            msg = f'``model={self.model}``, the argument ``time_window`` must be a postive integer'
            raise ValueError(msg)

        datagen_params = dict(
            scale=self.scale, 
            batch_size=self.global_batch_size,
            topography=self.topography, 
            landocean=self.landocean, 
            patch_size=self.patch_size, 
            model=self.model, 
            interpolation=self.interpolation,
            time_window=self.time_window)
        self.ds_train = DataGenerator(self.data_train, predictors=self.predictors_train, **datagen_params)
        self.ds_val = DataGenerator(self.data_val, predictors=self.predictors_val, **datagen_params)
        self.ds_test = DataGenerator(self.data_test, predictors=self.predictors_test, **datagen_params)

    def setup_model(self):
        """Setting up the model
        """
        ### number of channels
        if self.model in ['resnet_spc', 'resnet_bi', 'resnet_rc']:
            n_channels = self.data_train.shape[-1]
            if self.topography is not None:
                n_channels += 1
            if self.landocean is not None:
                n_channels += 1
            if self.predictors_train is not None:
                n_channels += len(self.predictors_train)
        elif self.model in ['recurrent_resnet_spc', 'recurrent_resnet_rc', 'recurrent_resnet_bi']:
            n_var_channels = self.data_train.shape[-1]
            n_st_channels = 0
            if self.predictors_train is not None:
                n_var_channels += len(self.predictors_train)
            if self.topography is not None:
                n_st_channels += 1
            if self.landocean is not None:
                n_st_channels += 1
            n_channels = (n_var_channels, n_st_channels)

        ### instantiating and fitting the model
        if self.model == 'resnet_spc':
            self.model = resnet_spc(scale=self.scale, n_channels=n_channels, **self.architecture_params)
        elif self.model == 'resnet_rc':
            self.model = resnet_rc(scale=self.scale, n_channels=n_channels, **self.architecture_params)
        elif self.model == 'resnet_bi':
            self.model = resnet_bi(n_channels=n_channels, **self.architecture_params)        
        elif self.model == 'recurrent_resnet_spc':
            self.model = recurrent_resnet_spc(scale=self.scale, n_channels=n_channels, 
                                              time_window=self.time_window, **self.architecture_params)
        elif self.model == 'recurrent_resnet_rc':
            self.model = recurrent_resnet_rc(scale=self.scale, n_channels=n_channels, 
                                             time_window=self.time_window, **self.architecture_params)
        elif self.model == 'recurrent_resnet_bi':
            self.model = recurrent_resnet_bi(n_channels=n_channels, time_window=self.time_window, 
                                            **self.architecture_params)

        if self.verbose == 1 and self.running_on_first_worker:
            self.model.summary(line_length=150)

    def run(self):
        """Compiling, training and saving the model
        """
        ### Setting up the optimizer
        if isinstance(self.learning_rate, tuple):
            ### Adam optimizer with a scheduler 
            self.learning_rate = PiecewiseConstantDecay(boundaries=[self.lr_decay_after], 
                                                        values=[self.learning_rate[0], 
                                                                self.learning_rate[1]])
        elif isinstance(self.learning_rate, float):
            # as in Goyan et al 2018 (https://arxiv.org/abs/1706.02677)
            self.learning_rate *= hvd.size()
        self.optimizer = Adam(learning_rate=self.learning_rate)

        ### Callbacks
        # early stopping
        callbacks = []
        if self.early_stopping:
            earlystop = EarlyStopping(monitor='val_loss', mode='min', patience=self.patience, 
                                      min_delta=self.min_delta, verbose=self.verbose)
            callbacks.append(earlystop)
        # loss plotting
        if self.plot == 'llp':
            plotlosses = livelossplot.PlotLossesKerasTF()
            callbacks.append(plotlosses) 

        # Horovod: add Horovod DistributedOptimizer.
        self.optimizer = hvd.DistributedOptimizer(self.optimizer)
        # Horovod: broadcast initial variable states from rank 0 to all other processes.
        # This is necessary to ensure consistent initialization of all workers when
        # training is started with random weights or restored from a checkpoint.
        callbacks.append(hvd.callbacks.BroadcastGlobalVariablesCallback(0))
        
        # verbosity for model.fit
        if self.verbose == 1 and self.running_on_first_worker:
            verbose = 1
        elif self.verbose == 2 and self.running_on_first_worker:
            verbose = 2
        else:
            verbose = 0

        # Model checkopoints are saved at the end of every epoch, if it's the best seen so far.
        if self.savecheckpoint_path is not None:
            os.makedirs(self.savecheckpoint_path, exist_ok=True)
            model_checkpoint_callback = tf.keras.callbacks.ModelCheckpoint(
                os.path.join(self.savecheckpoint_path, './checkpoint_epoch-{epoch:02d}.h5'),
                save_weights_only=False,
                monitor='val_loss',
                mode='min',
                save_best_only=True)
            # Horovod: save checkpoints only on worker 0 to prevent other workers from corrupting them.
            if self.running_on_first_worker:
                callbacks.append(model_checkpoint_callback)

        ### Compiling and training the model
        if self.steps_per_epoch is not None:
            self.steps_per_epoch = self.steps_per_epoch // hvd.size()

        self.model.compile(optimizer=self.optimizer, loss=self.lossf)
        self.fithist = self.model.fit(
            self.ds_train, 
            epochs=self.epochs, 
            steps_per_epoch=self.steps_per_epoch,
            validation_data=self.ds_val, 
            validation_steps=self.validation_steps, 
            verbose=self.verbose, 
            callbacks=callbacks,
            use_multiprocessing=self.use_multiprocessing)
        self.score = self.model.evaluate(
            self.ds_test, 
            steps=self.test_steps, 
            verbose=verbose)
        print(f'\nScore on the test set: {self.score}')
        
        self.timing.runtime()
        
        if self.plot == 'plt':
            if self.save_plot:
                learning_curve_fname = 'learning_curve.png'
            else:
                learning_curve_fname = None
            
            if self.running_on_first_worker:
                plot_history(self.fithist.history, path=learning_curve_fname)
                if self.show_plot:
                    show()

        if self.save:
            if self.save_path is None:
                save_path = './saved_model/'
        
            if self.running_on_first_worker:
                os.makedirs(save_path, exist_ok=True)
                self.model.save(save_path, save_format='tf')


