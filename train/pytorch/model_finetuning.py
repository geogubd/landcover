import numpy as np
import torch
import torch.nn as nn
import json
from train.pytorch.models.unet import Unet
from train.pytorch.models.fusionnet import Fusionnet
import time
from torch.optim import lr_scheduler
import copy
from train.pytorch.utils.eval_segm import mean_IoU
from train.pytorch.losses import (multiclass_ce, multiclass_dice_loss, multiclass_jaccard_loss, multiclass_tversky_loss)
from train.pytorch.data_loader import DataGenerator
from torch.utils import data
import os

class GroupParams(nn.Module):

    def __init__(self, model):
        super(GroupParams, self).__init__()
        self.gammas = nn.Parameter(torch.ones((1, 32, 1, 1)))
        self.betas = nn.Parameter(torch.zeros((1, 32, 1, 1)))
        self.model = model

    def forward(self, x):
        x, conv1_out, conv1_dim = self.model.down_1(x)
        x = x * self.gammas + self.betas

        x, conv2_out, conv2_dim = self.model.down_2(x)

        x, conv3_out, conv3_dim = self.model.down_3(x)
        x, conv4_out, conv4_dim = self.model.down_4(x)

        # Bottleneck
        x = self.model.conv5_block(x)

        # up layers
        x = self.model.up_1(x, conv4_out, conv4_dim)
        x = self.model.up_2(x, conv3_out, conv3_dim)
        x = self.model.up_3(x, conv2_out, conv2_dim)
        x = self.model.up_4(x, conv1_out, conv1_dim)


        return self.model.conv_final(x)

def finetune(path_2_saved_model, loss, gen_loaders,params, n_epochs=25):
    opts = params["model_opts"]
    unet = Unet(opts)
    checkpoint = torch.load(path_2_saved_model)
    unet.load_state_dict(checkpoint['model'])
    unet.eval()
    for param in unet.parameters():
        param.requires_grad = False

    # Parameters of newly constructed modules have requires_grad=True by default
    model_2_finetune = GroupParams(unet)
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    model_2_finetune = model_2_finetune.to(device)
    loss = loss().to(device)


    # Observe that only parameters of final layer are being optimized as
    # opposed to before.
    optimizer = torch.optim.SGD(model_2_finetune.parameters(), lr=0.01, momentum=0.9)

    # Decay LR by a factor of 0.1 every 7 epochs
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=7, gamma=0.1)

    model_2_finetune = train_model(model_2_finetune, loss, optimizer,
                             exp_lr_scheduler, gen_loaders, num_epochs=n_epochs)
    return model_2_finetune

def train_model(model, criterion, optimizer, scheduler, dataloaders, num_epochs=25):
    since = time.time()

    best_model_wts = copy.deepcopy(model.state_dict())
    best_acc = 0.0
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    for epoch in range(num_epochs):
        print('Epoch {}/{}'.format(epoch, num_epochs - 1))
        print('-' * 10)

        # Each epoch has a training and validation phase
        for phase in ['train', 'val']:
            if phase == 'train':
                scheduler.step()
                model.train()  # Set model to training mode
            else:
                model.eval()   # Set model to evaluate mode

            running_loss = 0.0
            val_meanIoU = 0.0
            n_iter = 0

            # Iterate over data.
            for inputs, labels in dataloaders[phase]:
                inputs = inputs[:, :, 2:240 - 2, 2:240 - 2]
                labels = labels[:, :, 94:240 - 94, 94:240 - 94]
                inputs = inputs.to(device)
                labels = labels.to(device)

                # zero the parameter gradients
                optimizer.zero_grad()

                # forward
                # track history if only in train
                with torch.set_grad_enabled(phase == 'train'):
                    outputs = model.forward(inputs)
                    loss = criterion(torch.squeeze(labels,1).long(), outputs)

                    # backward + optimize only if in training phase
                    if phase == 'train':
                        loss.backward()
                        optimizer.step()

                # statistics
                running_loss += loss.item()
                n_iter+=1
                if phase == 'val':
                    y_hr = np.squeeze(labels.cpu().numpy(), axis=1)
                    batch_size, _, _ = y_hr.shape
                    y_hat = outputs.cpu().numpy()
                    y_hat = np.argmax(y_hat, axis=1)
                    batch_meanIoU = 0
                    for j in range(batch_size):
                        batch_meanIoU += mean_IoU(y_hat[j], y_hr[j])
                    batch_meanIoU /= batch_size
                    val_meanIoU += batch_meanIoU

            epoch_loss = running_loss / n_iter
            epoch_acc = val_meanIoU /n_iter

            print('{} Loss: {:.4f} Acc: {:.4f}'.format(
                phase, epoch_loss, epoch_acc))

            # deep copy the model
            if phase == 'val' and epoch_acc > best_acc:
                best_acc = epoch_acc
                best_model_wts = copy.deepcopy(model.state_dict())

        print()

    time_elapsed = time.time() - since
    print('Training complete in {:.0f}m {:.0f}s'.format(
        time_elapsed // 60, time_elapsed % 60))
    print('Best val Acc: {:4f}'.format(best_acc))

    # load best model weights
    model.load_state_dict(best_model_wts)
    return model

def main():
    params = json.load(open("/mnt/blobfuse/train-output/conditioning/models/backup_unet_gn_runningstats4/training/params.json", "r"))
    training_patches_fn = "data/ny_1m_2013_finetuning_train.txt"
    validation_patches_fn = "data/ny_1m_2013_finetuning_val.txt"
    f = open(training_patches_fn, "r")
    training_patches = f.read().strip().split("\n")
    f.close()

    f = open(validation_patches_fn, "r")
    validation_patches = f.read().strip().split("\n")
    f.close()

    batch_size = params["loader_opts"]["batch_size"]
    patch_size = params["patch_size"]
    num_channels = params["loader_opts"]["num_channels"]
    params_train = {'batch_size': params["loader_opts"]["batch_size"],
                    'shuffle': params["loader_opts"]["shuffle"],
                    'num_workers': params["loader_opts"]["num_workers"]}

    training_set = DataGenerator(
        training_patches, batch_size, patch_size, num_channels, superres=params["train_opts"]["superres"]
    )

    validation_set = DataGenerator(
        validation_patches, batch_size, patch_size, num_channels, superres=params["train_opts"]["superres"]
    )


    train_opts = params["train_opts"]
    model_opts = params["model_opts"]

    # Default model is Duke_Unet


    if train_opts["loss"] == "dice":
        loss = multiclass_dice_loss
    elif train_opts["loss"] == "ce":
        loss = multiclass_ce
    elif train_opts["loss"] == "jaccard":
        loss = multiclass_jaccard_loss
    elif train_opts["loss"] == "tversky":
        loss = multiclass_tversky_loss
    else:
        print("Option {} not supported. Available options: dice, ce, jaccard, tversky".format(train_opts["loss"]))
        raise NotImplementedError

    path = "/mnt/blobfuse/train-output/conditioning/models/backup_unet_gn_runningstats4/training/checkpoint_best.pth.tar"

    dataloaders = {'train': data.DataLoader(training_set, **params_train), 'val': data.DataLoader(validation_set, **params_train)}

    model = finetune(path, loss, dataloaders, params, n_epochs=10)

    savedir = "/mnt/blobfuse/train-output/conditioning/models/finetuning/"
    if not os.path.exists(savedir):
        os.makedirs(savedir)

    if model_opts["model"] == "unet":
        finetunned_fn = savedir + "finetuned_unet_gn.pth.tar"
    torch.save(model.state_dict(), finetunned_fn)

if __name__ == "__main__":
    main()
