""" This file defines an agent for the PR2 ROS environment. """
import copy

import rospy

from gps.agent.agent import Agent
from gps.agent.agent_utils import generate_noise, setup
from gps.agent.config import AGENT_ROS
from gps.agent.ros.ros_utils import ServiceEmulator, msg_to_sample, \
        policy_to_msg
from gps.proto.gps_pb2 import TRIAL_ARM, AUXILIARY_ARM
from gps_agent_pkg.msg import TrialCommand, SampleResult, PositionCommand, \
        RelaxCommand, DataRequest


class AgentROS(Agent):
    """
    All communication between the algorithms and ROS is done through
    this class.
    """
    def __init__(self, hyperparams, init_node=True):
        """
        Initialize agent.
        Args:
            hyperparams: Dictionary of hyperparameters.
            init_node: Whether or not to initialize a new ROS node.
        """
        config = copy.deepcopy(AGENT_ROS)
        config.update(hyperparams)
        Agent.__init__(self, config)
        if init_node:
            rospy.init_node('gps_agent_ros_node')
        self._init_pubs_and_subs()
        self._seq_id = 0  # Used for setting seq in ROS commands.

        conditions = self._hyperparams['conditions']

        self.x0 = []
        for field in ('x0', 'ee_points_tgt', 'reset_conditions'):
            self._hyperparams[field] = setup(self._hyperparams[field],
                                             conditions)
        self.x0 = self._hyperparams['x0']

        r = rospy.Rate(1)
        r.sleep()

    def _init_pubs_and_subs(self):
        self._trial_service = ServiceEmulator(
            self._hyperparams['trial_command_topic'], TrialCommand,
            self._hyperparams['sample_result_topic'], SampleResult
        )
        self._reset_service = ServiceEmulator(
            self._hyperparams['reset_command_topic'], PositionCommand,
            self._hyperparams['sample_result_topic'], SampleResult
        )
        self._relax_service = ServiceEmulator(
            self._hyperparams['relax_command_topic'], RelaxCommand,
            self._hyperparams['sample_result_topic'], SampleResult
        )
        self._data_service = ServiceEmulator(
            self._hyperparams['data_request_topic'], DataRequest,
            self._hyperparams['sample_result_topic'], SampleResult
        )

    def _get_next_seq_id(self):
        self._seq_id = (self._seq_id + 1) % (2 ** 32)
        return self._seq_id

    def get_data(self, arm=TRIAL_ARM):
        """
        Request for the most recent value for data/sensor readings.
        Returns entire sample report (all available data) in sample.
        Args:
            arm: TRIAL_ARM or AUXILIARY_ARM.
        """
        request = DataRequest()
        request.id = self._get_next_seq_id()
        request.arm = arm
        request.stamp = rospy.get_rostime()
        result_msg = self._data_service.publish_and_wait(request)
        # TODO - Make IDs match, assert that they match elsewhere here.
        sample = msg_to_sample(result_msg, self)
        return sample

    # TODO - The following could be more general by being relax_actuator
    #        and reset_actuator.
    def relax_arm(self, arm):
        """
        Relax one of the arms of the robot.
        Args:
            arm: Either TRIAL_ARM or AUXILIARY_ARM.
        """
        relax_command = RelaxCommand()
        relax_command.id = self._get_next_seq_id()
        relax_command.stamp = rospy.get_rostime()
        relax_command.arm = arm
        self._relax_service.publish_and_wait(relax_command)

    def reset_arm(self, arm, mode, data):
        """
        Issues a position command to an arm.
        Args:
            arm: Either TRIAL_ARM or AUXILIARY_ARM.
            mode: An integer code (defined in gps_pb2).
            data: An array of floats.
        """
        reset_command = PositionCommand()
        reset_command.mode = mode
        reset_command.data = data
        reset_command.pd_gains = self._hyperparams['pid_params']
        reset_command.arm = arm
        timeout = self._hyperparams['trial_timeout']
        reset_command.id = self._get_next_seq_id()
        self._reset_service.publish_and_wait(reset_command, timeout=timeout)
        #TODO: Maybe verify that you reset to the correct position.

    def reset(self, condition):
        """
        Reset the agent for a particular experiment condition.
        Args:
            condition: An index into hyperparams['reset_conditions'].
        """
        condition_data = self._hyperparams['reset_conditions'][condition]
        self.reset_arm(TRIAL_ARM, condition_data[TRIAL_ARM]['mode'],
                       condition_data[TRIAL_ARM]['data'])
        self.reset_arm(AUXILIARY_ARM, condition_data[AUXILIARY_ARM]['mode'],
                       condition_data[AUXILIARY_ARM]['data'])

    def sample(self, policy, condition, verbose=True, save=True):
        """
        Reset and execute a policy and collect a sample.
        Args:
            policy: A Policy object.
            condition: Which condition setup to run.
            verbose: Unused for this agent.
            save: Whether or not to store the trial into the samples.
        Returns:
            sample: A Sample object.
        """
        self.reset(condition)

        # Generate noise.
        noise = generate_noise(self.T, self.dU, self._hyperparams)

        # Execute trial.
        trial_command = TrialCommand()
        trial_command.id = self._get_next_seq_id()
        trial_command.controller = policy_to_msg(policy, noise)
        trial_command.T = self.T
        trial_command.id = self._get_next_seq_id()
        trial_command.frequency = self._hyperparams['frequency']
        ee_points = self._hyperparams['end_effector_points']
        trial_command.ee_points = ee_points.reshape(ee_points.size).tolist()
        trial_command.ee_points_tgt = \
                self._hyperparams['ee_points_tgt'][condition].tolist()
        trial_command.state_datatypes = self._hyperparams['state_include']
        trial_command.obs_datatypes = self._hyperparams['state_include']
        sample_msg = self._trial_service.publish_and_wait(
            trial_command, timeout=self._hyperparams['trial_timeout']
        )

        sample = msg_to_sample(sample_msg, self)
        if save:
            self._samples[condition].append(sample)
        return sample
