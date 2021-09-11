import pytorch_lightning as pl
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger
import torch
import torch.nn.functional as F
import torch.nn as nn
import matplotlib.pyplot as plt
from utils import TriStageLRSchedule
from utils import posterior2pianoroll, extract_notes_wo_velocity, transcription_accuracy
from utils.text_processing import GreedyDecoder
import fastwer
import contextlib
from nnAudio.Spectrogram import MelSpectrogram
import pandas as pd

class SimpleASR(pl.LightningModule):
    def __init__(self, spec_layer, text_transform, lr):
        super().__init__()
        self.text_transform = text_transform        
#         self.save_hyperparameters() #
        self.lr = lr
        
        self.spec_layer = spec_layer
        self.embedding = nn.Linear(80,256)
        self.bilstm = nn.LSTM(256, 256, batch_first=True, num_layers=2, bidirectional=True)
        self.classifier = nn.Linear(256*2, 62)

    def forward(self, x):
        spec = self.spec_layer(x) # (B, F, T)
        spec = torch.log(spec+1e-8)
        spec = spec.transpose(1,2) # (B, T, F)
        x = self.embedding(spec)
        x, _ = self.bilstm(x)
        pred = self.classifier(x)
        
        pred = torch.log_softmax(pred, -1) # CTC loss requires log_softmax
        
        output = {"prediction": pred,
                  "spectrogram": spec}
        return output

    def training_step(self, batch, batch_idx):
        x = batch['waveforms']
        output = self(x)
        pred = output["prediction"]
        loss = F.ctc_loss(pred.transpose(0, 1),
                          batch['labels'],
                          batch['input_lengths'],
                          batch['label_lengths'])        
        self.log("train_ctc_loss", loss)
        return loss

    def validation_step(self, batch, batch_idx):
        x = batch['waveforms']
        with torch.no_grad():
            output = self(x)
            pred = output["prediction"]
            spec = output["spectrogram"]
            loss = F.ctc_loss(pred.transpose(0, 1),
                              batch['labels'],
                              batch['input_lengths'],
                              batch['label_lengths'])
            valid_metrics = {"valid_ctc_loss": loss}

            pred = pred.cpu().detach()
            decoded_preds, decoded_targets = GreedyDecoder(pred,
                                                           batch['labels'],
                                                           batch['label_lengths'],
                                                           self.text_transform)
            PER_batch = fastwer.score(decoded_preds, decoded_targets)/100            
            valid_metrics['PER'] = PER_batch
            if batch_idx==0:
                self.log_images(spec, f'Valid/spectrogram')
                self._log_test(decoded_preds, 'Valid/texts_pred', max_sentences=4)
                if self.current_epoch==0: # log ground truth
                    self._log_test(decoded_targets, 'Valid/texts_label', max_sentences=4)

            self.log_dict(valid_metrics)

    def _log_test(self, texts, tag, max_sentences=4):
        text_list=[]
        for idx in range(min(len(texts),max_sentences)): # visualize 4 samples or the batch whichever is smallest
            # Avoid using <> tag, which will have conflicts in html markdown
            text_list.append(texts[idx])
        s = pd.Series(text_list, name="IPA")
        self.logger.experiment.add_text(tag, s.to_markdown(), global_step=self.current_epoch)     

    def test_step(self, batch, batch_idx):
        x = batch['audio']
        y = batch['frame']
        metrics = {}


        with torch.no_grad():
            pred = self(x)
            max_timesteps = pred.size(1)
            y = y[:,:max_timesteps]
            loss = F.binary_cross_entropy(pred, y)
            metrics["test_loss/frame"] = loss.item()

            pred = pred.cpu().detach()[0]
            y = y.cpu().detach()[0]

            self.transcription_accuracy(pred, y, metrics)
        self.log_dict(metrics)            


    def log_images(self, tensor, key):
        for idx, spec in enumerate(tensor):
            fig, ax = plt.subplots(1,1)
            ax.imshow(spec.cpu().detach().t(), aspect='auto', origin='lower')    
            self.logger.experiment.add_figure(f"{key}/{idx}", fig, global_step=self.current_epoch)         


    def configure_optimizers(self):

        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)
#         scheduler = TriStageLRSchedule(optimizer,
#                                        [1e-8, self.lr, 1e-8],
#                                        [0.2,0.6,0.2],
#                                        max_update=len(self.train_dataloader.dataloader)*self.trainer.max_epochs)   
#         scheduler = MultiStepLR(optimizer, [1,3,5,7,9], gamma=0.1, last_epoch=-1, verbose=False)

#         return [optimizer], [{"scheduler":scheduler, "interval": "step"}]
        return [optimizer]