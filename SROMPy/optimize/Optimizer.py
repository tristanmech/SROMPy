# Copyright 2018 United States Government as represented by the Administrator of
# the National Aeronautics and Space Administration. No copyright is claimed in
# the United States under Title 17, U.S. Code. All Other Rights Reserved.

# The Stochastic Reduced Order Models with Python (SROMPy) platform is licensed
# under the Apache License, Version 2.0 (the "License"); you may not use this
# file except in compliance with the License. You may obtain a copy of the
# License at http://www.apache.org/licenses/LICENSE-2.0.

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
Class to solve SROM optimization problem.
"""

import numpy as np
import scipy.optimize as opt
import time
import imp

from SROMPy.optimize import ObjectiveFunction
from SROMPy.optimize import Gradient


# ------------Helper funcs for scipy optimize-----------------------------
def scipy_objective_function(x, objective_function, gradient, samples):
    """
    Function to pass to scipy minimize defining objective. Wraps the
    ObjectiveFunction.evaluate() function that defines SROM error. Need to
    unpack design variables x into samples & probabilities. Handle two cases:

    1) joint optimization over samples & probabilities (samples=None)
    2) sequential optimization -> optimize probabilities for fixed samples
    """

    # Unpacking simple with samples are fixed:
    probabilities = x

    error = objective_function.evaluate(samples, probabilities)

    return error


def scipy_gradient(x, objective_function, gradient, samples):
    """
    Function to pass to scipy minimize defining objective. Wraps the
    ObjectiveFunction.evaluate() function that defines SROM error. Need to
    unpack design variables x into samples & probabilities. Handle two cases:

    1) joint optimization over samples & probabilities (samples=None)
    2) sequential optimization -> optimize probabilities for fixed samples
    """

    # Unpacking simple with samples are fixed:
    probabilities = x

    gradient = gradient.evaluate(samples, probabilities)
    return gradient

# -----------------------------------------------------------------


class Optimizer:
    """
    Class that delegates the construction of an SROM through the optimization
    of the SROM parameters (samples/probabilities) that minimize the error
    between SROM & target random vector
    """

    def __init__(self, target, srom, obj_weights=None,
                 error='SSE', max_moment=5, cdf_grid_pts=100):
        """
        inputs:
            -target - initialized RandomVector object (either
            AnalyticRandomVector or SampleRandomVector)
            -obj_weights - array of floats defining the relative weight of the
                terms in the objective function. Terms are error in moments,
                CDFs, and correlation matrix in that order. Default will give
                each term equal weight
            -error - string 'mean' or 'max' defining how error is defined
                between the statistics of the SROM & target
            -max_moment - int, max order to evaluate moment errors up to
            -cdf_grid_pts - int, # pts to evaluate CDF errors on

        """

        self._target = target

        # Initialize objective function defining SROM vs target error.
        self._srom_objective_function = ObjectiveFunction(srom, target,
                                                          obj_weights, error,
                                                          max_moment,
                                                          cdf_grid_pts)

        self._srom_gradient = Gradient(srom, target, obj_weights, error,
                                       max_moment, cdf_grid_pts)

        # Get srom size & dimension.
        self._srom_size = srom.size
        self._dim = srom.dim

        # Gradient only available for SSE error obj function.
        if error.upper() == "SSE":
            self._grad = scipy_gradient
        else:
            self._grad = None

        self.__detect_parallelization()

    def get_optimal_params(self, num_test_samples=500,  method=None,
                           joint_opt=False, output_interval=10, verbose=True):
        """
        Solve the SROM optimization problem - finds samples & probabilities
        that minimize the error between SROM/Target RV statistics.

        inputs:
            -joint_opt, bool, Flag for optimizing jointly for samples &
                probabilities rather than sequentially (draw samples then
                optimize probabilities in loop - default).
            -num_test_samples, int, If optimizing sequentially (samples then
                probabilities), this is number of random sample sets to test in
                opt
            -tol, float, tolerance of scipy optimization algorithm
            -options, dict, options for scipy optimization algorithm
            -method, str, method specifying scipy optimization algorithm
            -output_interval, int, how often to print optimization progress
            -verbose: bool. Flag for whether to generate text output.

        returns optimal SROM samples & probabilities

        """

        if not isinstance(num_test_samples, int):
            raise TypeError("Number of test samples must be a positive int.")

        if num_test_samples <= 0:
            raise ValueError("Insufficient number of test samples specified.")

        # Report whether we're running in sequential or parallel mode.
        if verbose:
            self.show_parallelization_information(num_test_samples)

        # Find optimal parameters.
        t0 = time.time()
        optimal_samples, optimal_probabilities = \
            self.__perform_optimization(num_test_samples,
                                        joint_opt,
                                        method,
                                        output_interval,
                                        verbose)

        # Display final errors in statistics:
        moment_error, cdf_error, correlation_error, mean_error = \
            self.get_errors(optimal_samples, optimal_probabilities)

        if verbose and self.cpu_rank == 0:
            print("\tOptimization time: ", time.time() - t0, "seconds")
            print("\tFinal SROM errors:")
            print("\t\tCDF: ", cdf_error)
            print("\t\tMoment: ", moment_error)
            print("\t\tCorrelation: ", correlation_error)

        return optimal_samples, optimal_probabilities

    # -----Helper funcs----

    def __perform_optimization(self, num_test_samples, joint_opt, method,
                               output_interval, verbose):
        """
        Calls optimization loop function and, in the case of parallelization,
        acquires the optimal results achieved across all CPUs before
        returning them.
        -num_test_samples: int, If optimizing sequentially (samples then
                probabilities), this is number of random sample sets to test in
                opt
        -joint_opt: bool, Flag for optimizing jointly for samples &
                probabilities rather than sequentially (draw samples then
                optimize probabilities in loop - default).
        -method: str, method specifying scipy optimization algorithm
        -output_interval: int, how often to print optimization progress
        -verbose: bool. Flag for whether to generate text output.

        returns optimal SROM samples & probabilities
        """

        optimal_samples, optimal_probabilities = \
            self.__run_optimization_loop(num_test_samples,
                                         joint_opt,
                                         method,
                                         output_interval,
                                         verbose)

        # If we're running in parallel mode, we need to gather all of the data
        # across CPUs and identify the best result.
        if self.number_CPUs > 1:

            optimal_samples, optimal_probabilities = \
                self.__get_optimal_parallel_results(optimal_samples,
                                                    optimal_probabilities)

        return optimal_samples, optimal_probabilities

    def __run_optimization_loop(self, num_test_samples, joint_opt,
                                method, output_interval, verbose):
        """
        Is run by __perform_optimization to perform sampling and acquire
        optimal parameter values.

        Calls optimization loop function and, in the case of parallelization,
        acquires the optimal results achieved across all CPUs before
        returning them.
        -num_test_samples: int, If optimizing sequentially (samples then
                probabilities), this is number of random sample sets to test in
                opt.
        -joint_opt: bool, Flag for optimizing jointly for samples &
                probabilities rather than sequentially (draw samples then
                optimize probabilities in loop - default).
        -method: str, method specifying scipy optimization algorithm
        -output_interval: int, how often to print optimization progress
        -verbose: bool. Flag for whether to generate text output.

        returns optimal SROM samples & probabilities
        """

        # Track optimal func value with corresponding samples/probabilities.
        optimal_probabilities = None
        optimal_samples = None
        best_objective_function_result = 1e6

        np.random.seed(self.cpu_rank)
        num_test_samples_per_cpu = num_test_samples // self.number_CPUs

        # Perform sampling, tracking the best results.
        for i in range(num_test_samples_per_cpu):

            # Randomly draw new.
            srom_samples = self._target.draw_random_sample(self._srom_size)

            # Optimize using scipy.
            args = (self._srom_objective_function,
                    self._srom_gradient,
                    srom_samples)

            optimization_result = \
                opt.minimize(scipy_objective_function,
                             self.get_initial_guess(joint_opt),
                             args=args,
                             jac=self._grad,
                             constraints=self.get_constraints(joint_opt),
                             method=method,
                             bounds=self.get_param_bounds(joint_opt))

            # If error is lower than lowest so far, keep track of results.
            if optimization_result['fun'] < best_objective_function_result:
                optimal_samples = srom_samples
                optimal_probabilities = optimization_result['x']
                best_objective_function_result = optimization_result['fun']

            # Report ongoing results to user if in sequential mode.
            if verbose and self.number_CPUs == 1 and \
                    (i == 0 or (i + 1) % output_interval == 0):

                print("\tIteration %d Objective Function: %s" % \
                      (i + 1, optimization_result['fun']))

                print("Optimal:", best_objective_function_result)

        return optimal_samples, optimal_probabilities

    def __get_optimal_parallel_results(self, optimal_samples,
                                       optimal_probabilities):
        """
        Allows all CPUs to share results data to determine optimum. Optimal
        results are then distributed to all CPUs and returned.
        Note: should only be run when multiple CPUs are utilized to compute
        optimization and mpi4py module is available.
        -optimal_samples: samples computed in get_optimal_params
        -optimal_probabilities: probabilities computed in
               get_optimal_params

        returns tuple containing optimal samples and probabilities
        """

        # Create a package to transmit results in.
        this_cpu_results = {'samples': optimal_samples,
                            'probabilities': optimal_probabilities}

        # Gather results.
        import mpi4py
        comm = mpi4py.MPI.COMM_WORLD
        all_cpu_results = comm.gather(this_cpu_results, root=0)

        # Let CPU 0 gather and compare results to determine optimum.
        if self.cpu_rank == 0:

            best_mean_error = 1e6

            for result in all_cpu_results:

                result_moment_error, result_cdf_error, \
                    result_correlation_error, result_mean_error = \
                    self.get_errors(optimal_samples, optimal_probabilities)

                if result_mean_error < best_mean_error:
                    best_mean_error = result_mean_error
                    optimal_samples = result['samples']
                    optimal_probabilities = result['probabilities']

        # Now send optimal results from CPU 0 to all CPUs.
        optimal_samples, optimal_probabilities = \
            comm.broadcast([optimal_samples, optimal_probabilities], root=0)

        return optimal_samples, optimal_probabilities

    def show_parallelization_information(self, num_test_samples):
        """
        Displays whether sequential or parallel optimization is running,
        and shows a warning if the number samples cannot be equally
        distributed among the available number of CPUs.
        -num_test_samples: Total number of test samples to be run.
        """

        if self.number_CPUs == 1:
            print("SROM Sequential Optimizer:")

        elif self.cpu_rank == 0:
            print("SROM Parallel Optimizer (%s cpus):" % self.number_CPUs)

        if self.cpu_rank == 0 and \
           num_test_samples % self.number_CPUs != 0:

            print("Warning: # test samples not divisible by # CPUs!")
            print("%s per core, %s total" % \
                  (num_test_samples // self.number_CPUs, num_test_samples))

    def __detect_parallelization(self):
        """
        Detects whether multiple processors are available and sets
        self.number_CPUs and self.cpu_rank accordingly.
        """

        try:
            imp.find_module('mpi4py')

            from mpi4py import MPI
            comm = MPI.COMM_WORLD

            self.number_CPUs = comm.size
            self.cpu_rank = comm.rank

        except ImportError:

            self.number_CPUs = 1
            self.cpu_rank = 0

    def get_errors(self, samples, probabilities):
        """
        Compute moment, cdf, correlation, and mean error for computed samples
        and probabilities.
        -samples: samples computed in get_optimal_params
        -probabilities: probabilities computed in get_optimal_params
        returns tuple of moment error, cdf error, correlation error, and
                 mean error.
        """

        result_moment_error = \
            self._srom_objective_function.get_moment_error(samples,
                                                           probabilities)

        result_cdf_error = \
            self._srom_objective_function.get_cdf_error(samples, probabilities)

        result_correlation_error = \
            self._srom_objective_function.get_corr_error(samples, probabilities)

        result_mean_error = np.mean([result_moment_error,
                                     result_cdf_error,
                                     result_correlation_error])

        return (result_moment_error, result_cdf_error, result_correlation_error,
                result_mean_error)

    def get_param_bounds(self, joint_opt):
        """
        Get the bounds on parameters for SROM optimization problem. If doing
        joint optimization, need bounds for both samples & probabilities. If
        not, just need trivial bounds on probabilities
        """
        
        if not joint_opt:
            bounds = [(0.0, 1.0)] * self._srom_size
        else:
            raise NotImplementedError("SROM joint optimization not implemented")

        return bounds
        
    def get_constraints(self, joint_opt):
        """
        Returns constraint dictionaries for scipy optimize that enforce 
        probabilities summing to 1 for joint or sequential optimize case
        """

        # A little funky, need to return function as constraint.
        # TODO - use lambda function instead?

        # Sequential case - unknown vector x is probabilities directly
        def seq_constraint(x):
            return 1.0 - np.sum(x)

        # Joint case - probabilities at end of unknown vector x
        def joint_constraint(x):
            return 1.0 - np.sum(x[self._srom_size * self._dim:])

        if not joint_opt:
            return {'type': 'eq', 'fun': seq_constraint}
        else:
            return {'type': 'eq', 'fun': joint_constraint,
                    'args': (self._srom_size, self._dim)}

    def get_initial_guess(self, joint_opt):
        """
        Return initial guess for optimization. Randomly drawn samples w/ equal
        probability for joint optimization or just equal probabilities for 
        sequential optimization
        """
    
        if joint_opt:
            # Randomly draw some samples & stack them with probabilities
            # TODO - untested
            samples = self._target.draw_random_sample(self._srom_size)

            probabilities = (1. /
                             float(self._srom_size)) * np.ones(self._srom_size)

            initial_guess = np.hstack((samples.flatten(), probabilities))
        else:
            initial_guess = \
                (1. / float(self._srom_size)) * np.ones(self._srom_size)

        return initial_guess
