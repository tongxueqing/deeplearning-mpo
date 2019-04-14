from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import os
import os.path
import imp
import datetime
import shutil
import time
#import tensorflow.python.platform
import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
import re

import sys

import shutil

import input_data
from hyper_parameters import *

net = None

#tf.set_random_seed(FLAGS.random_seed)
np.random.seed(FLAGS.random_seed)

def batch(image, label, batch_size, name):
    b_images, b_labels=  tf.train.batch([image, label],
                                        batch_size=batch_size,
                                        num_threads=8,
                                        capacity=3 * FLAGS.batch_size + 20,
                                        name=name)
    tf.summary.image('sumary/images/' + name, b_images)
    return b_images, b_labels

def tower_loss_and_eval(images, labels, train_phase, reuse=None, cpu_variables=False):
    with tf.variable_scope('inference', reuse=reuse):
        logits = net.inference(images, train_phase, cpu_variables=cpu_variables)
    losses = net.losses(logits, labels)
    total_loss = tf.add_n(losses, name='total_loss')

    # Add weight decay to the loss.
    total_loss = total_loss + FLAGS.WEIGHT_DECAY * tf.add_n(
        [tf.nn.l2_loss(v) for v in tf.trainable_variables()])

    loss_averages = tf.train.ExponentialMovingAverage(0.99, name='avg')
    loss_averages_op = loss_averages.apply(losses + [total_loss])

    for l in losses + [total_loss]:
        loss_name = l.op.name
        tf.summary.scalar(loss_name + ' (raw)', l)
        tf.summary.scalar(loss_name, loss_averages.average(l))

    with tf.control_dependencies([loss_averages_op]):
        total_loss = tf.identity(total_loss)
    evaluation = net.evaluation(logits, labels)
    return total_loss, evaluation

def average_gradients(tower_grads):
    average_grads = []
    for grad_and_vars in zip(*tower_grads):
        grads = []
        for g, _ in grad_and_vars:
            expanded_g = tf.expand_dims(g, 0)
            grads.append(expanded_g)
        grad = tf.concat(grads, 0)
        grad = tf.reduce_mean(grad, 0)
        grad_and_var = (grad, grad_and_vars[0][1])
        average_grads.append(grad_and_var)
    return average_grads

def run_training(restore_chkpt=None):
    global net
    net = imp.load_source('net', FLAGS.net_module)
    with tf.Graph().as_default(), tf.device('/cpu:0'):
        train_phase = tf.Variable(True, trainable=False, name='train_phase', dtype=tf.bool, collections=[])

        inp_data = input_data.get_input_data(FLAGS)

        t_image, t_label = inp_data['train']['image_input'], inp_data['train']['label_input']
        t_image = input_data.aug_train(t_image, inp_data['aux'])

        v_image, v_label = inp_data['validation']['image_input'], inp_data['validation']['label_input']
        v_image = input_data.aug_eval(v_image, inp_data['aux'])

        v_images, v_labels = batch(v_image, v_label, FLAGS.batch_size * FLAGS.num_gpus, 'eval_batch')


        v_images_split = tf.split(v_images, FLAGS.num_gpus)
        v_labels_split = tf.split(v_labels, FLAGS.num_gpus)

        global_step = tf.get_variable('global_step',
                                      [],
                                      initializer=tf.constant_initializer(0),
                                      trainable=False)
        epoch_steps = inp_data['train']['images'].shape[0] / (FLAGS.batch_size)
        decay_steps = int(FLAGS.num_epochs_per_decay * epoch_steps)

        lr = tf.train.exponential_decay(FLAGS.initial_learning_rate,
                                        global_step,
                                        decay_steps,
                                        FLAGS.learning_rate_decay_factor,
                                        staircase=True)
        # boundaries = [int(epoch_steps * epoch) for epoch in learning_rate_decay_boundary]
        # values = [FLAGS.initial_learning_rate*decay for decay in learning_rate_decay_value]
        # lr = tf.train.piecewise_constant(tf.cast(global_step, tf.int32), boundaries, values)

        opt = tf.train.MomentumOptimizer(learning_rate=lr, momentum=FLAGS.MOMENTUM,
                                         name='optimizer', use_nesterov=True)
        # opt = tf.train.AdamOptimizer(lr, name='optimizer')

        tower_grads = []
        tower_evals = []
        tower_losses = []
        cpu_variables = FLAGS.num_gpus > 1
        for i in range(FLAGS.num_gpus):
            reuse = i > 0
            with tf.device('/gpu:%d' % i):
                with tf.name_scope('tower_%d' % i) as scope:

                    t_images, t_labels = batch(t_image, t_label, FLAGS.batch_size, 'train_batch')

                    images, labels = tf.cond(train_phase,
                                             lambda: (t_images, t_labels),
                                             lambda: (v_images_split[i], v_labels_split[i]))


                    loss, evaluation = tower_loss_and_eval(images, labels, train_phase, reuse, cpu_variables)
                    tower_losses.append(loss)
                    tower_evals.append(evaluation)


                    summaries = tf.get_collection(tf.GraphKeys.SUMMARIES, scope)

                    grads = opt.compute_gradients(loss)

                    tower_grads.append(grads)

        # We must calculate the mean of each gradient. Note that this is the
        # synchronization point across all towers.
        grads = average_gradients(tower_grads)

        summaries.append(tf.summary.scalar('learning_rate', lr))
        for grad, var in grads:
            if grad is not None:
                summaries.append(tf.summary.histogram('gradients/' + var.op.name, grad))
        # Apply the gradients to adjust the shared variables.
        apply_gradients_op = opt.apply_gradients(grads, global_step=global_step)
        with tf.control_dependencies([apply_gradients_op]):
            normalize_gs = global_step.assign_add(FLAGS.num_gpus - 1)

        for var in tf.trainable_variables():
            summaries.append(tf.summary.histogram('variables/' + var.op.name, var))


        train_loss = tf.Variable(5.0, trainable=False, name='train_loss', dtype=tf.float32)
        train_precision = tf.Variable(0.0, trainable=False, name='train_precision', dtype=tf.float32)

        train_lp_decay = 0.9
        train_lp_updates = []
        for i in range(FLAGS.num_gpus):
            train_lp_updates.append(train_loss.assign_sub((1.0 - train_lp_decay) * (train_loss - tower_losses[i])))
            new_precision = tf.reduce_mean(tf.cast(tower_evals[i], tf.float32))
            train_lp_updates.append(train_precision.assign_sub((1.0 - train_lp_decay) * (train_precision - new_precision)))
        train_lp_update = tf.group(*train_lp_updates)

        summaries.append(tf.summary.scalar('loss/train', train_loss))
        summaries.append(tf.summary.scalar('precision/train', train_precision))

        validation_loss = tf.Variable(0.0, trainable=False, dtype=tf.float32)
        validation_precision = tf.Variable(0.0, trainable=False, dtype=tf.float32)
        assign_ph = tf.placeholder(tf.float32, shape=[])

        vl_assign_op = validation_loss.assign(assign_ph)
        vp_assign_op = validation_precision.assign(assign_ph)

        summaries.append(tf.summary.scalar('loss/validation', validation_loss))
        summaries.append(tf.summary.scalar('precision/validation', validation_precision))


        variable_averages =  tf.train.ExponentialMovingAverage(0.9, global_step, zero_debias=True)
        variables_averages_op = variable_averages.apply(tf.trainable_variables())

        train_op = tf.group(apply_gradients_op, normalize_gs, variables_averages_op, train_lp_update)

        qrunners = tf.get_collection(tf.GraphKeys.QUEUE_RUNNERS)
        for qr in qrunners:
            summaries.append(tf.summary.scalar('queues/size/' + qr.name, qr.queue.size()))

        saver = tf.train.Saver(tf.global_variables())
        ema_saver = tf.train.Saver(variable_averages.variables_to_restore())

        summary_op = tf.summary.merge(summaries)

        init = tf.global_variables_initializer()

        switch_train = train_phase.assign(True)
        switch_eval = train_phase.assign(False)

        sess = tf.Session(config=tf.ConfigProto(
            allow_soft_placement=True,
            log_device_placement=FLAGS.log_device_placement))

        #initialize const variables with dataset
        sess.run(inp_data['initializer'], feed_dict=inp_data['init_feed'])

        sess.run(init)

        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)



        summary_writer = tf.summary.FileWriter(FLAGS.log_dir, sess.graph)



        sys.stdout.write('\n\n')
        epoch_steps = int(inp_data['train']['images'].shape[0] / FLAGS.batch_size + 0.5)
        start_epoch = 0
        if restore_chkpt is not None:
            saver.restore(sess, restore_chkpt)
            sys.stdout.write('Previously started training session restored from "%s".\n' % restore_chkpt)
            start_epoch = int(sess.run(global_step)) // epoch_steps

        print_hyper_parameters()
        sys.stdout.write('Starting with epoch #%d.\n' % (start_epoch + 1))
        bestValidationPrecision = 0.0
        for epoch in range(start_epoch, FLAGS.max_epochs):
            sys.stdout.write('\n')
            _ = sess.run(switch_train)

            lr_val = sess.run(opt._learning_rate)
            sys.stdout.write('Epoch #%d. [Train], learning rate: %.2e\n' % (epoch + 1, lr_val))
            sys.stdout.flush()
            cum_t = 0.0
            step = 0
            log_steps = FLAGS.log_steps

            fmt_str = 'Epoch #%d [%s]. Step %d/%d (%d%%). Speed = %.2f sec/b, %.2f img/sec. Batch_loss = %.3f. Batch_precision = %.3f'
            while step < epoch_steps:
                start_time = time.time()
                _, loss_value, eval_value = sess.run([train_op, loss, evaluation])
                duration = time.time() - start_time

                step += FLAGS.num_gpus

                assert not np.isnan(loss_value), 'Model diverged with loss = NaN'
                cum_t += duration
                sec_per_batch = duration / FLAGS.num_gpus
                img_per_sec = FLAGS.num_gpus * FLAGS.batch_size / duration

                if cum_t > 2.0:
                    cum_t = 0.0
                    sys.stdout.write('\r')
                    sys.stdout.write(fmt_str %
                        (epoch + 1,
                        'Train',
                        step + 1,
                        epoch_steps,
                        int(100.0 * (step + 1) / epoch_steps),
                        sec_per_batch,
                        img_per_sec,
                        loss_value,
                        np.mean(eval_value) * 100.0
                        ))
                    sys.stdout.flush()

                log_steps -= FLAGS.num_gpus
                if (log_steps < 0):
                    log_steps = FLAGS.log_steps
                    summary_str = sess.run(summary_op)
                    glob_step = epoch * epoch_steps + step
                    summary_writer.add_summary(summary_str, glob_step)

            sys.stdout.write('\r')
            sys.stdout.write(fmt_str %
                (epoch + 1,
                'Train',
                epoch_steps,
                epoch_steps,
                100,
                sec_per_batch,
                img_per_sec,
                loss_value,
                np.mean(eval_value) * 100.0
                ))

            sys.stdout.write('\n')
            train_loss_val, train_precision_val = sess.run([train_loss, train_precision])
            sys.stdout.write('Epoch #%d. Train loss = %.3f. Train precision = %.3f.\n' %
                (epoch + 1,
                train_loss_val,
                train_precision_val * 100.0))
            checkpoint_path = os.path.join(FLAGS.log_dir, 'model.ckpt')
            chkpt = saver.save(sess, checkpoint_path, global_step=global_step)
            sys.stdout.write('Checkpoint "%s" saved.\n\n' % chkpt)

            #Evaluation phase
            sess.run(switch_eval)
            sys.stdout.write('Epoch #%d. [Evaluation]\n' % (epoch + 1))
            ema_saver.restore(sess, chkpt)
            sys.stdout.write('EMA variables restored.\n')


            eval_cnt = inp_data['validation']['images'].shape[0]

            eval_steps = (eval_cnt + FLAGS.batch_size - 1) // FLAGS.batch_size
            eval_correct = 0
            eval_loss = 0.0
            cum_t = 0.0
            while eval_cnt > 0:
                start_time = time.time()
                eval_values_and_losses = sess.run(tower_evals + tower_losses)
                duration = time.time() - start_time

                eval_values = eval_values_and_losses[:FLAGS.num_gpus]
                eval_values = np.concatenate(eval_values, axis=0)

                eval_losses = eval_values_and_losses[-FLAGS.num_gpus:]

                cnt = min(eval_values.shape[0], eval_cnt)

                eval_correct += np.sum(eval_values[:cnt])
                eval_loss += np.sum(eval_losses) * FLAGS.batch_size

                eval_cnt -= cnt

                cur_step = eval_steps - (eval_cnt + FLAGS.batch_size - 1) // FLAGS.batch_size
                sec_per_batch = duration / FLAGS.num_gpus
                img_per_sec = FLAGS.num_gpus * FLAGS.batch_size / duration

                cum_t += duration

                if cum_t > 0.5:
                    cum_t = 0.0
                    sys.stdout.write('\r')
                    sys.stdout.write(fmt_str %
                        (epoch + 1,
                        'Evaluation',
                        cur_step,
                        eval_steps,
                        int(100.0 * cur_step / eval_steps),
                        sec_per_batch,
                        img_per_sec,
                        eval_losses[-1],
                        np.mean(eval_values) * 100.0
                        ))
                    sys.stdout.flush()

            sys.stdout.write('\r')
            sys.stdout.write(fmt_str %
                (epoch + 1,
                'Evaluation',
                eval_steps,
                eval_steps,
                int(100.0),
                sec_per_batch,
                img_per_sec,
                eval_losses[-1],
                np.mean(eval_values) * 100.0
                ))
            sys.stdout.write('\n')
            sys.stdout.flush()

            eval_precision = eval_correct / inp_data['validation']['images'].shape[0]
            eval_loss = eval_loss / inp_data['validation']['images'].shape[0]
            if eval_precision > bestValidationPrecision:
                bestValidationPrecision = eval_precision
            sys.stdout.write('Epoch #%d. Validation loss = %.3f. Validation precision = %.3f. '
                             'Best precision = %.3f\n' %
                (epoch + 1,
                eval_loss,
                eval_precision * 100.0,
                bestValidationPrecision * 100))

            saver.restore(sess, chkpt)
            sys.stdout.write('Variables restored.\n\n')

            sess.run(vl_assign_op, feed_dict={assign_ph: eval_loss})
            sess.run(vp_assign_op, feed_dict={assign_ph: eval_precision})

            # w = os.get_terminal_size().columns
            w = 40
            sys.stdout.write(('=' * w + '\n') * 2)
        bestFile = open(os.path.join(FLAGS.log_dir, 'best.txt'), 'w')
        bestFile.write('Best precision = %.4f\n' % bestValidationPrecision)
        bestFile.close()

        coord.request_stop()
        coord.join(threads)

def main(_):
    latest_chkpt = tf.train.latest_checkpoint(FLAGS.log_dir)
    if latest_chkpt is not None:
        while True:
            sys.stdout.write('Checkpoint "%s" found. Continue last training session?\n' % latest_chkpt)
            sys.stdout.write('Continue - [c/C]. Restart (all content of log dir will be removed) - [r/R]. Abort - [a/A].\n')
            ans = input().lower()
            if len(ans) == 0:
                continue
            if ans[0] == 'c':
                break
            elif ans[0] == 'r':
                latest_chkpt = None
                shutil.rmtree(FLAGS.log_dir)
                break
            elif ans[0] == 'a':
                return

    run_training(restore_chkpt=latest_chkpt)

if __name__ == '__main__':
    tf.app.run()
