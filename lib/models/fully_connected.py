import torch.nn as nn
from model import Model
from lib.modules.latent_levels import FullyConnectedLatentLevel
from lib.modules.networks import FullyConnectedNetwork
from lib.modules.layers import FullyConnectedLayer
from lib.distributions import Normal, Bernoulli, Multinomial
from lib.modules.misc import LayerNorm


class FullyConnectedModel(Model):
    """
    A hierarchical fully-connected latent variable model.

    Args:
        model_config (dict): dictionary containing model configuration params
    """
    def __init__(self, model_config):
        super(FullyConnectedModel, self).__init__(model_config)
        self._construct(**model_config)

    def _construct(self, inference_input_form, constant_variances_gen, n_latent,
                   n_layers_inf, n_layers_gen, n_units_inf, n_units_gen,
                   non_linearity_inf, non_linearity_gen, connection_type_inf,
                   connection_type_gen, batch_norm_inf, batch_norm_gen):
        """
        Method for constructing the model using the model configuration params.

        Args:
            inference_input_form (list): contains 'observation', 'gradient', and/or 'error'
            constant_variances_gen (boolean): whether to have learnable constant generated variances
            n_latent (list): number of latent variables at each level
            n_layers_inf (list): number of layers in the inference model at each level
            n_layers_gen (list): number of layers in the generative model at each level
            n_units_inf (list): number of units in the inference model at each level
            n_units_gen (list): number of units in the generative model at each level
            non_linearity_inf (str): inference non-linearity type
            non_linearity_gen (str): generative non-linearity type
            connection_type_inf (str): type of inference model connectivity
            connection_type_gen (str): type of generative model connectivity
            batch_norm_inf (boolean): whether to batch norm inference model
            batch_norm_gen (boolean): whether to batch norm generative model
        """
        self.inference_procedure = inference_input_form

        self.latent_levels = nn.ModuleList([])

        for level_ind in range(len(n_latent)):
            level_config = {}

            latent_config = {}
            latent_config['n_in'] = [n_units_inf[level_ind], n_units_gen[level_ind+1]]
            latent_config['n_variables'] = n_latent[level_ind]
            latent_config['inference_procedure'] = self.inference_procedure
            level_config['latent_config'] = latent_config

            level_config['inference_procedure'] = self.inference_procedure

            level_config['inference_config'] = {'n_in': None,
                                                'n_units': n_units_inf[level_ind],
                                                'connection_type': connection_type_inf,
                                                'non_linearity': non_linearity_inf,
                                                'batch_norm': batch_norm_inf}

            level_config['generative_config'] = {'n_in': n_latent[level_ind+1],
                                                 'n_units': n_units_gen[level_ind+1],
                                                 'connection_type': connection_type_gen,
                                                 'non_linearity': non_linearity_gen,
                                                 'batch_norm': batch_norm_gen}

            latent_level = FullyConnectedLatentLevel(level_config)
            self.latent_levels.append(latent_level)


    def _get_encoding_form(self, observation):
        """
        Gets the appropriate input form for the inference procedure.

        Args:
            observation (tensor): the input observation
        """
        encoding = []
        if 'observation' in self.inference_procedure:
            encoding.append(observation - 0.5)
        if 'gradient' in self.inference_procedure:
            grads = self.latent_levels[0].latent.approx_posterior_gradients()
            grads = torch.cat([LayerNorm()(grad) for grad in grads], dim=1)
            params = self.latent_levels[0].latent.approx_posterior_parameters()
            params = torch.cat([LayerNorm()(param) for param in params], dim=1)
            grads_params = torch.cat([grads, params], dim=1)
            encoding.append(grads_params)
        if 'error' in self.inference_procedure:
            errors = [self._output_error(observation), self.latent_levels[0].latent.error()]
            errors = torch.cat([LayerNorm()(error) for error in errors], dim=1)
            params = self.latent_levels[0].latent.approx_posterior_parameters()
            params = torch.cat([LayerNorm()(param) for param in params], dim=1)
            errors_params = torch.cat([errors, params], dim=1)
            encoding.append(errors_params)
        return encoding[0] if len(encoding) == 1 else torch.cat(encoding, dim=1)

    def _output_error(self, observation):
        """
        Calculates Gaussian error for encoding.

        Args:
            observation (tensor): observation to use for error calculation
        """
        output_mean = self.output_dist.mean.detach()
        n_samples = output_mean.data.shape[1]
        if len(observation.data.shape) == 2:
            observation = observation.unsqueeze(1).repeat(1, n_samples, 1)
        if type(self.output_dist) == Normal:
            output_log_var = self.output_dist.log_var.detach()
            n_error = (observation - output_mean) / torch.exp(output_log_var + 1e-7)
        elif type(self.output_dist) == Bernoulli:
            ## TODO
            raise NotImplementedError
        elif type(self.output_dist) == Multinomial:
            raise NotImplementedError
        n_error = n_error.mean(dim=1)
        return n_error

    def infer(self, observation):
        """
        Method for perfoming inference of the approximate posterior over the
        latent variables.

        Args:
            observation (tensor): observation to infer latent variables from
        """
        encoding = self._get_encoding_form(observation)
        for level in self.latent_levels:
            encoding = level.infer(encoding)

    def generate(self, gen=False, n_samples=1):
        """
        Method for generating observations, i.e. running the generative model.

        Args:
            gen (boolean): whether to sample from prior or approximate posterior
            n_samples (int): number of samples to draw and evaluate
        """
        decoding = None
        for level in self.latent_levels[::-1]:
            decoding = level.generate(decoding, gen, n_samples)
        decoding = self.output_network(decoding)
        self.output_dist.mean = self.output_mean(decoding)
        if type(self.output_dist) == Normal:
            self.output_dist.log_var = self.output_log_var(decoding)
        return self.output_dist.sample()

    def re_init(self):
        """
        Method for reinitializing the latent variables.
        """
        self.generate(gen=True)
        for level in self.latent_levels:
            level.latent.re_init()

    def inference_parameters(self):
        """
        Method for obtaining the inference model parameters.
        """
        params = nn.ParameterList()
        for level in self.latent_levels:
            params.extend(list(level.inference_parameters()))
        return params

    def generative_parameters(self):
        """
        Method for obtaining the generative model parameters.
        """
        params = nn.ParameterList()
        for level in self.latent_levels:
            params.extend(list(level.generative_parameters()))
        params.extend(list(self.output_network.parameters()))
        params.extend(list(self.output_mean.parameters))
        if type(self.output_dist) == Normal:
            params.extend(list(self.output_log_var.parameters()))
        return params
