"""
TO-DO:
    * test r-int
    * residual discriminator with arbitrary scaling factors. Fix cropping2d and strides (4x or 5x)
    * save the generator predictions on a refernce image from the test set, once per epoch
    * training with Horovod

Conditional GAN, as described in "Image-to-Image Translation with Conditional Adversarial Networks"
"""

import time
import os 
import datetime
import tensorflow as tf
import numpy as np

from .resnet_int import rint
from .resnet_spc import rspc
from .discriminator import residual_discriminator
from .dataloader import create_pair_hr_lr
from .utils import Timing


def generator_loss(disc_generated_output, gen_output, target, lambda_scaling_factor=100):
    """
    Generator loss:
    * It is a sigmoid cross entropy loss of the generated images and an array of ones.
    * The paper also includes L1 loss which is MAE (mean absolute error) between the generated image and the target image.
    * This allows the generated image to become structurally similar to the target image.
    * The formula to calculate the total generator loss = gan_loss + LAMBDA * l1_loss, where LAMBDA = 100. This value was decided by the authors of the paper.
    """
    binary_crossentropy = tf.keras.losses.BinaryCrossentropy(from_logits=True)
    # binary crossentropy
    gan_loss = binary_crossentropy(tf.ones_like(disc_generated_output), disc_generated_output)
    # mean absolute error, regularization
    l1_loss = tf.reduce_mean(tf.abs(target - gen_output))
    total_gen_loss = gan_loss + (lambda_scaling_factor * l1_loss)
    return total_gen_loss, gan_loss, l1_loss


def discriminator_loss(disc_real_output, disc_generated_output):
    """
    Discriminator loss:
    * The discriminator loss function takes 2 inputs; real images, generated images
    * real_loss is a sigmoid cross entropy loss of the real images and an array of ones(since these are the real images)
    * generated_loss is a sigmoid cross entropy loss of the generated images and an array of zeros(since these are the fake images)
    * Then the total_loss is the sum of real_loss and the generated_loss
    """
    binary_crossentropy = tf.keras.losses.BinaryCrossentropy(from_logits=True)  
    real_loss = binary_crossentropy(tf.ones_like(disc_real_output), disc_real_output)
    generated_loss = binary_crossentropy(tf.zeros_like(disc_generated_output), disc_generated_output)
    total_disc_loss = real_loss + generated_loss
    return total_disc_loss


@tf.function
def train_step(lr_array, hr_array, generator, discriminator, generator_optimizer, 
               discriminator_optimizer, epoch, summary_writer):
    """
    Training:
    * For each example input generate an output.
    * The discriminator receives the input_image and the generated image as the first input. The second input is the input_image and the target_image.
    * Next, we calculate the generator and the discriminator loss.
    * Then, we calculate the gradients of loss with respect to both the generator and the discriminator variables(inputs) and apply those to the optimizer.
    """
    lr_array = tf.cast(lr_array[tf.newaxis,...], tf.float32)
    hr_array =  tf.cast(hr_array[tf.newaxis,...], tf.float32)
    
    with tf.GradientTape() as gen_tape, tf.GradientTape() as disc_tape:
        # running the generator
        gen_lr_array = generator(lr_array, training=True)
        # running the discriminator using both the reference and generated HR images
        disc_real_output = discriminator([lr_array, hr_array], training=True)
        disc_generated_output = discriminator([lr_array, gen_lr_array], training=True)
        # computing the losses
        gen_total_loss, gen_gan_loss, gen_l1_loss = generator_loss(disc_generated_output, gen_lr_array, hr_array)
        disc_loss = discriminator_loss(disc_real_output, disc_generated_output)

    generator_gradients = gen_tape.gradient(gen_total_loss,
                                            generator.trainable_variables)
    discriminator_gradients = disc_tape.gradient(disc_loss,
                                                 discriminator.trainable_variables)

    generator_optimizer.apply_gradients(zip(generator_gradients,
                                            generator.trainable_variables))
    discriminator_optimizer.apply_gradients(zip(discriminator_gradients,
                                                discriminator.trainable_variables))

    with summary_writer.as_default():
        tf.summary.scalar('gen_total_loss', gen_total_loss, step=epoch)
        tf.summary.scalar('gen_gan_loss', gen_gan_loss, step=epoch)
        tf.summary.scalar('gen_l1_loss', gen_l1_loss, step=epoch)
        tf.summary.scalar('disc_loss', disc_loss, step=epoch)
        
    return gen_total_loss, gen_gan_loss, gen_l1_loss, disc_loss 


def training_cgan(x_train, epochs, model='rspc', scale=5, patch_size=50, interpolation='bicubic', topography=None, 
                  landocean=None, checkpoints_frequency=5, n_res_blocks=(20, 4), n_filters=64, attention=False):
    """
    
    Parameters
    ----------
    
    checkpoints_frequency : int, optional
        The training loop saves a checkpoint every ``checkpoints_frequency`` epochs.
    
    """
    timing = Timing()

    gentotal = []
    gengan = []
    genl1 = []
    disc = []
    
    log_dir = "cgan_logs/"
    summary_writer = tf.summary.create_file_writer(log_dir + "fit/" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    
    n_channels = 1
    if topography is not None:
        n_channels += 1
    if landocean is not None:
        n_channels += 1

    # generator
    if model == 'rspc':
        generator = rspc(scale=scale, n_channels=n_channels, n_filters=n_filters, 
                         n_res_blocks=n_res_blocks[0], n_channels_out=1, attention=attention)
    elif model == 'rint':
        generator = rint(n_channels=n_channels, n_filters=n_filters, 
                         n_res_blocks=n_res_blocks[0], n_channels_out=1, attention=attention)
        
    # discriminator
    discriminator = residual_discriminator(n_channels=n_channels, n_filters=n_filters, 
                                           n_res_blocks=n_res_blocks[1], model=model, attention=attention)
    
    # optimizers
    generator_optimizer = tf.keras.optimizers.Adam(2e-4, beta_1=0.5)
    discriminator_optimizer = tf.keras.optimizers.Adam(2e-4, beta_1=0.5)
    
    # checkpoint
    if checkpoints_frequency is not None:
        checkpoint_dir = './cgan_checkpoints'
        checkpoint_prefix = os.path.join(checkpoint_dir, "ckpt")
        checkpoint = tf.train.Checkpoint(generator_optimizer=generator_optimizer,
                                         discriminator_optimizer=discriminator_optimizer,
                                         generator=generator, discriminator=discriminator)
    
    for epoch in range(epochs):
        start = time.time()        
        print("Epoch: ", epoch)
        n_samples = 10 #x_train.shape[0]
        
        for i in range(n_samples):
            if (i + 1) % 100 == 0:
                print('.', end='')
       
            hr_array, lr_array = create_pair_hr_lr(x_train[i], tuple_predictors=None, scale=scale, topography=topography, 
                                                   landocean=landocean, patch_size=patch_size, model=model, 
                                                   interpolation=interpolation)

            losses = train_step(lr_array, hr_array, generator, discriminator, generator_optimizer, 
                                discriminator_optimizer, epoch, summary_writer)
            gen_total_loss, gen_gan_loss, gen_l1_loss, disc_loss = losses
        
        print()
        msg = 'gen_total_loss={:.5f}, gen_gan_loss={:.5f}, gen_l1_loss={:.5f}, disc_loss={:.5f}'
        print(msg.format(gen_total_loss, gen_gan_loss, gen_l1_loss, disc_loss))
        gentotal.append(gen_total_loss)
        gengan.append(gen_gan_loss)
        genl1.append(gen_l1_loss)
        disc.append(disc_loss)
        
        if checkpoints_frequency is not None:
            if (epoch + 1) % checkpoints_frequency == 0:
                checkpoint.save(file_prefix = checkpoint_prefix)

        print ('Time taken for epoch {} is {} sec\n'.format(epoch + 1, time.time()-start))
    checkpoint.save(file_prefix = checkpoint_prefix)

    losses_array = np.array((gentotal, gengan, genl1, disc))
    np.save('./losses.npy', losses_array)

    timing.runtime()

    return generator