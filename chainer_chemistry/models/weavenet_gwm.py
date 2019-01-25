import chainer
from chainer import functions
from chainer import links

from chainer_chemistry.config import MAX_ATOMIC_NUM
from chainer_chemistry.config import WEAVE_DEFAULT_NUM_MAX_ATOMS
from chainer_chemistry.links.embed_atom_id import EmbedAtomID

from chainer_chemistry.models import gwm
from chainer_chemistry.models.gwm import GWM


WEAVENET_DEFAULT_WEAVE_CHANNELS = [50, ]


def readout(a, mode='sum', axis=1):
    if mode == 'sum':
        a = functions.sum(a, axis=axis)
    elif mode == 'max':
        a = functions.max(a, axis=axis)
    elif mode == 'summax':
        a_sum = functions.sum(a, axis=axis)
        a_max = functions.max(a, axis=axis)
        a = functions.concat((a_sum, a_max), axis=axis)
    else:
        raise ValueError('mode {} is not supported'.format(mode))
    return a


class LinearLayer(chainer.Chain):

    def __init__(self, n_channel, n_layer):
        super(LinearLayer, self).__init__()
        with self.init_scope():
            self.layers = chainer.ChainList(
                *[links.Linear(None, n_channel) for _ in range(n_layer)]
            )
        self.n_output_channel = n_channel

    def forward(self, x):
        n_batch, n_atom, n_channel = x.shape
        x = functions.reshape(x, (n_batch * n_atom, n_channel))
        for l in self.layers:
            x = l(x)
            x = functions.relu(x)
        x = functions.reshape(x, (n_batch, n_atom, self.n_output_channel))
        return x


class AtomToPair(chainer.Chain):
    def __init__(self, n_channel, n_layer, n_atom):
        super(AtomToPair, self).__init__()
        with self.init_scope():
            self.linear_layers = chainer.ChainList(
                *[links.Linear(None, n_channel) for _ in range(n_layer)]
            )
        self.n_atom = n_atom
        self.n_channel = n_channel

    def forward(self, x):
        n_batch, n_atom, n_feature = x.shape
        atom_repeat = functions.reshape(x, (n_batch, 1, n_atom, n_feature))
        atom_repeat = functions.broadcast_to(
            atom_repeat, (n_batch, n_atom, n_atom, n_feature))
        atom_repeat = functions.reshape(atom_repeat,
                                        (n_batch, n_atom * n_atom, n_feature))

        atom_tile = functions.reshape(x, (n_batch, n_atom, 1, n_feature))
        atom_tile = functions.broadcast_to(
            atom_tile, (n_batch, n_atom, n_atom, n_feature))
        atom_tile = functions.reshape(atom_tile,
                                      (n_batch, n_atom * n_atom, n_feature))

        pair_x0 = functions.concat((atom_tile, atom_repeat), axis=2)
        pair_x0 = functions.reshape(pair_x0,
                                    (n_batch * n_atom * n_atom, n_feature * 2))
        for l in self.linear_layers:
            pair_x0 = l(pair_x0)
            pair_x0 = functions.relu(pair_x0)
        pair_x0 = functions.reshape(pair_x0,
                                    (n_batch, n_atom * n_atom, self.n_channel))

        pair_x1 = functions.concat((atom_repeat, atom_tile), axis=2)
        pair_x1 = functions.reshape(pair_x1,
                                    (n_batch * n_atom * n_atom, n_feature * 2))
        for l in self.linear_layers:
            pair_x1 = l(pair_x1)
            pair_x1 = functions.relu(pair_x1)
        pair_x1 = functions.reshape(pair_x1,
                                    (n_batch, n_atom * n_atom, self.n_channel))
        return pair_x0 + pair_x1


class PairToAtom(chainer.Chain):
    def __init__(self, n_channel, n_layer, n_atom, mode='sum'):
        super(PairToAtom, self).__init__()
        with self.init_scope():
            self.linearLayer = chainer.ChainList(
                *[links.Linear(None, n_channel) for _ in range(n_layer)]
            )
        self.n_atom = n_atom
        self.n_channel = n_channel
        self.mode = mode

    def forward(self, x):
        n_batch, n_pair, n_feature = x.shape
        a = functions.reshape(
            x, (n_batch * (self.n_atom * self.n_atom), n_feature))
        for l in self.linearLayer:
            a = l(a)
            a = functions.relu(a)
        a = functions.reshape(a, (n_batch, self.n_atom, self.n_atom,
                                  self.n_channel))
        a = readout(a, mode=self.mode, axis=2)
        return a


class WeaveModule(chainer.Chain):

    def __init__(self, n_atom, output_channel, n_sub_layer,
                 readout_mode='sum'):
        super(WeaveModule, self).__init__()
        with self.init_scope():
            self.atom_layer = LinearLayer(output_channel, n_sub_layer)
            self.pair_layer = LinearLayer(output_channel, n_sub_layer)
            self.atom_to_atom = LinearLayer(output_channel, n_sub_layer)
            self.pair_to_pair = LinearLayer(output_channel, n_sub_layer)
            self.atom_to_pair = AtomToPair(output_channel, n_sub_layer, n_atom)
            self.pair_to_atom = PairToAtom(output_channel, n_sub_layer, n_atom,
                                           mode=readout_mode)
        self.n_atom = n_atom
        self.n_channel = output_channel
        self.readout_mode = readout_mode

    def forward(self, atom_x, pair_x, atom_only=False):
        a0 = self.atom_to_atom.forward(atom_x)
        a1 = self.pair_to_atom.forward(pair_x)
        a = functions.concat([a0, a1], axis=2)
        next_atom = self.atom_layer.forward(a)
        next_atom = functions.relu(next_atom)
        if atom_only:
            return next_atom

        p0 = self.atom_to_pair.forward(atom_x)
        p1 = self.pair_to_pair.forward(pair_x)
        p = functions.concat([p0, p1], axis=2)
        next_pair = self.pair_layer.forward(p)
        next_pair = functions.relu(next_pair)
        return next_atom, next_pair


class WeaveNet_GWM(chainer.Chain):
    """WeaveNet implementation

    Args:
        weave_channels (list): list of int, output dimension for each weave
            module
        hidden_dim (int): hidden dim
        n_atom (int): number of atom of input array
        n_sub_layer (int): number of layer for each `AtomToPair`, `PairToAtom`
            layer
        n_atom_types (int): number of atom id
        readout_mode (str): 'sum' or 'max' or 'summax'
    """

    def __init__(self, weave_channels=None, hidden_dim=16,hidden_dim_super=16,
                 n_layers=4, n_heads=8,
                 n_super_feature=4 + 2 + 4 + MAX_ATOMIC_NUM*2,
                 n_atom=WEAVE_DEFAULT_NUM_MAX_ATOMS,
                 n_sub_layer=1, n_atom_types=MAX_ATOMIC_NUM,
                 readout_mode='sum',
                 dropout_ratio=0.5,
                 weight_tying=True,
                 scaler_mgr_flag=False,):
        weave_channels = weave_channels or WEAVENET_DEFAULT_WEAVE_CHANNELS
        weave_module = [
            WeaveModule(n_atom, c, n_sub_layer, readout_mode=readout_mode)
            for c in weave_channels
        ]

        super(WeaveNet_GWM, self).__init__()
        with self.init_scope():
            self.embed = EmbedAtomID(out_size=hidden_dim, in_size=n_atom_types)
            self.embed_super = chainer.links.Linear(in_size=n_super_feature, out_size=hidden_dim_super)

            self.weave_module = chainer.ChainList(*weave_module)

            # GWM
            self.gwm = GWM(hidden_dim=hidden_dim, hidden_dim_super=hidden_dim_super,
                           n_layers=n_layers, n_heads=n_heads,
                           dropout_ratio=dropout_ratio,
                           tying_flag=weight_tying,
                           scaler_mgr_flag=scaler_mgr_flag,
                           gpu=-1)
            self.linear_for_concat_super = chainer.links.Linear(in_size=None, out_size=hidden_dim)
        self.readout_mode = readout_mode

        self.hidden_dim = hidden_dim
        self.hidden_dim_super = hidden_dim_super
        self.n_layers = n_layers
        self.n_heads = n_heads
        self.dropout_ratio = dropout_ratio
        self.weight_tying = weight_tying

    def __call__(self, atom_x, pair_x, super_node, train=True):
        if atom_x.dtype == self.xp.int32:
            # atom_array: (minibatch, atom)
            atom_x = self.embed(atom_x)

        # call reset for all RNN modules in GWM
        self.gwm.GRU_local.reset_state()
        self.gwm.GRU_super.reset_state()
        # ebmbed super node
        g = self.embed_super(super_node)

        for i in range(len(self.weave_module)):
            layer_index = 0 if self.weight_tying else i

            if i == len(self.weave_module) - 1:
                # last layer, only `atom_x` is needed.
                out_atom_x = self.weave_module[i].forward(atom_x, pair_x,
                                                      atom_only=True)
            else:
                # not last layer, both `atom_x` and `pair_x` are needed
                out_atom_x, pair_x = self.weave_module[i].forward(atom_x, pair_x)

            # GWM
            new_atom_x, new_g = self.gwm(atom_x, out_atom_x, g, layer_index)

            atom_x = new_atom_x
            g = new_g

        x = readout(atom_x, mode=self.readout_mode, axis=1)
        g2 = chainer.functions.concat((x, g))
        out_g = chainer.functions.relu(self.linear_for_concat_super(g2))

        return out_g
