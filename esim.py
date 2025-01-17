import os
# os.environ["CUDA_VISIBLE_DEVICES"] = "0, 1"
import pandas as pd
import numpy as np
from tqdm import tqdm
import time
import logging
from sklearn.model_selection import StratifiedKFold
from keras_bert import load_trained_model_from_checkpoint, Tokenizer
from keras.optimizers import Adam
import pandas as pd
from sklearn.metrics import mean_absolute_error, accuracy_score, f1_score
from keras.layers import *
from keras.models import Model
import keras.backend as K
from keras.callbacks import Callback
from keras.activations import softmax
learning_rate = 5e-5
min_learning_rate = 1e-5

config_path = 'chinese_L-12_H-768_A-12/bert_config.json'
checkpoint_path = 'chinese_L-12_H-768_A-12/bert_model.ckpt'
dict_path = 'chinese_L-12_H-768_A-12/vocab.txt'


MAX_LEN = 64

token_dict = {}
with open(dict_path, 'r', encoding='utf-8') as reader:
    for line in reader:
        token = line.strip()
        token_dict[token] = len(token_dict)
tokenizer = Tokenizer(token_dict)

train= pd.read_csv('baifendian_data/train.csv')
test=pd.read_csv('baifendian_data/dev_set.csv',sep='\t')
train_achievements = train['question1'].values
train_requirements = train['question2'].values
labels = train['label'].values
def label_process(x):
    if x==0:
        return [1,0]
    else:
        return [0,1]
train['label']=train['label'].apply(label_process)
labels_cat=list(train['label'].values)
labels_cat=np.array(labels_cat)
test_achievements = test['question1'].values
test_requirements = test['question2'].values
print(train.shape,test.shape)


class data_generator:
    def __init__(self, data, batch_size=16):
        self.data = data
        self.batch_size = batch_size
        self.steps = len(self.data[0]) // self.batch_size
        if len(self.data[0]) % self.batch_size != 0:
            self.steps += 1
    def __len__(self):
        return self.steps
    def __iter__(self):
        while True:
            X1, X2, y = self.data
            idxs = list(range(len(self.data[0])))
            np.random.shuffle(idxs)
            T, T_, Y = [], [], []
            for c, i in enumerate(idxs):
                achievements = X1[i]
                requirements = X2[i]
                t, t_ = tokenizer.encode(first=achievements, second=requirements, max_len=MAX_LEN)
                T.append(t)
                T_.append(t_)
                Y.append(y[i])
                if len(T) == self.batch_size or i == idxs[-1]:
                    T = np.array(T)
                    T_ = np.array(T_)
                    Y = np.array(Y)
                    yield [T, T_], Y
                    T, T_, Y = [], [], []
def apply_multiple(input_, layers):
    if not len(layers) > 1:
        raise ValueError('Layers list should contain more than 1 layer')
    else:
        agg_ = []
        for layer in layers:
            agg_.append(layer(input_))
        out_ = Concatenate()(agg_)
    return out_
def unchanged_shape(input_shape):
    return input_shape
def substract(input_1, input_2):
    neg_input_2 = Lambda(lambda x: -x, output_shape=unchanged_shape)(input_2)
    out_ = Add()([input_1, neg_input_2])
    return out_
def submult(input_1, input_2):
    mult = Multiply()([input_1, input_2])
    sub = substract(input_1, input_2)
    out_ = Concatenate()([sub, mult])
    return out_
def soft_attention_alignment(input_1, input_2):
    attention = Dot(axes=-1)([input_1, input_2])
    w_att_1 = Lambda(lambda x: softmax(x, axis=1),  ##soft max to each column
                     output_shape=unchanged_shape)(attention)
    w_att_2 = Permute((2, 1))(Lambda(lambda x: softmax(x, axis=2),  ## axis =2 soft max to each row
                                     output_shape=unchanged_shape)(attention))
    in1_aligned = Dot(axes=1)([w_att_1, input_1])
    in2_aligned = Dot(axes=1)([w_att_2, input_2])
    return in1_aligned, in2_aligned
def get_model():
    bert_model = load_trained_model_from_checkpoint(config_path, checkpoint_path)
    # for l in bert_model.layers:
    #     l.trainable = True

    T1 = Input(shape=(None,))
    T2 = Input(shape=(None,))

    tp1 = Lambda(lambda x: K.zeros_like(x))(T1)
    tp2 = Lambda(lambda x: K.zeros_like(x))(T2)
    x1 = bert_model([T1, tp1])
    x2 = bert_model([T2, tp2])
    X1 = Lambda(lambda x: x[:, 0:-1])(x1)
    X2 = Lambda(lambda x: x[:, 0:-1])(x2)

    encode = Bidirectional(LSTM(200, return_sequences=True))
    q1_encoded = encode(X1)
    q2_encoded = encode(X2)
    q1_aligned, q2_aligned = soft_attention_alignment(q1_encoded, q2_encoded)
    q1_combined = Concatenate()([q1_encoded, q2_aligned, submult(q1_encoded, q2_aligned)])
    q2_combined = Concatenate()([q2_encoded, q1_aligned, submult(q2_encoded, q1_aligned)])
    compose = Bidirectional(GRU(200, return_sequences=True))
    q1_compare = compose(q1_combined)
    q2_compare = compose(q2_combined)
    # Aggregate
    q1_rep = apply_multiple(q1_compare, [GlobalAvgPool1D(), GlobalMaxPool1D()])
    q2_rep = apply_multiple(q2_compare, [GlobalAvgPool1D(), GlobalMaxPool1D()])
    # Classifier
    merged = Concatenate()([q1_rep, q2_rep])
    dense = BatchNormalization()(merged)
    dense = Dense(30, activation='selu')(dense)

    dense = BatchNormalization()(dense)
    output = Dense(2, activation='softmax')(dense)
    model = Model([T1, T2], output)
    model.compile(
        loss='categorical_crossentropy',
        optimizer=Adam(1e-3),  # 用足够小的学习率
        metrics=['accuracy']
    )
    model.summary()
    return model


class Evaluate(Callback):
    def __init__(self, val_data, val_index):
        self.score = []
        self.best = 0.
        self.early_stopping = 0
        self.val_data = val_data
        self.val_index = val_index
        self.predict = []
        self.lr = 0
        self.passed = 0

    def on_batch_begin(self, batch, logs=None):
        """第一个epoch用来warmup，第二个epoch把学习率降到最低
        """
        if self.passed < self.params['steps']:
            self.lr = (self.passed + 1.) / self.params['steps'] * learning_rate
            K.set_value(self.model.optimizer.lr, self.lr)
            self.passed += 1
        elif self.params['steps'] <= self.passed < self.params['steps'] * 2:
            self.lr = (2 - (self.passed + 1.) / self.params['steps']) * (learning_rate - min_learning_rate)
            self.lr += min_learning_rate
            K.set_value(self.model.optimizer.lr, self.lr)
            self.passed += 1

    def on_epoch_end(self, epoch, logs=None):
        score, acc, f1 = self.evaluate()
        if score > self.best:
            self.best = score
            self.early_stopping = 0
            model.save_weights('bert{}.w'.format(fold))
        else:
            self.early_stopping += 1
    def evaluate(self):
        self.predict = []
        prob = []
        val_x1, val_x2, val_y, val_cat = self.val_data
        for i in tqdm(range(len(val_x1))):
            achievements = val_x1[i]
            requirements = val_x2[i]

            t1, t1_ = tokenizer.encode(first=achievements, second=requirements)
            T1, T1_ = np.array([t1]), np.array([t1_])
            _prob = model.predict([T1, T1_])
            oof_train[self.val_index[i]] = _prob[0]
            self.predict.append(np.argmax(_prob, axis=1)[0]+1)
            prob.append(_prob[0])

        score = 1.0 / (1 + mean_absolute_error(val_y+1, self.predict))
        acc = accuracy_score(val_y+1, self.predict)
        f1 = f1_score(val_y+1, self.predict, average='macro')
        return score, acc, f1

nfolds=10
skf = StratifiedKFold(n_splits=nfolds, shuffle=True, random_state=42)
def predict(data):
    prob = []
    val_x1, val_x2 = data
    for i in tqdm(range(len(val_x1))):
        achievements = val_x1[i]
        requirements = val_x2[i]

        t1, t1_ = tokenizer.encode(first=achievements, second=requirements)
        T1, T1_ = np.array([t1]), np.array([t1_])
        _prob = model.predict([T1, T1_])
        prob.append(_prob[0])
    return prob
oof_train = np.zeros((len(train), 2), dtype=np.float32)
oof_test = np.zeros((len(test), 2), dtype=np.float32)
for fold, (train_index, valid_index) in enumerate(skf.split(train_achievements, labels)):
    x1 = train_achievements[train_index]
    x2 = train_requirements[train_index]
    y = labels_cat[train_index]
    val_x1 = train_achievements[valid_index]
    val_x2 = train_requirements[valid_index]
    val_y = labels[valid_index]
    val_cat = labels_cat[valid_index]
    train_D = data_generator([x1, x2, y])
    evaluator = Evaluate([val_x1, val_x2, val_y, val_cat], valid_index)
    model = get_model()
    model.fit_generator(train_D.__iter__(),
                        steps_per_epoch=len(train_D),
                        epochs=7,
                        callbacks=[evaluator]
                       )
    model.load_weights('bert{}.w'.format(fold))
    oof_test += predict([test_achievements, test_requirements])
    K.clear_session()
oof_test /= nfolds
test=pd.DataFrame(oof_test)
test.to_csv('test_pred.csv',index=False)
test.head(),test.shape
train=pd.DataFrame(oof_train)
train.to_csv('train_pred.csv',index=False)


pred=pd.read_csv('test_pred.csv').values
pred=pred.argmax(axis=1)
sub=pd.DataFrame()
sub['pred']=list(pred)
sub.to_csv('sub.csv',sep='\t',header=None)
